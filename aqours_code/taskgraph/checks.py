"""Where the Coordinator's checks run: on the host, or in the workers' sandbox.

A run has four kinds of check: a node's check on its worktree, the same check
on the integration branch right after the node is merged, ``final_checks``,
and the hidden tests. They all run code that a worker wrote, so with
``--sandbox docker`` each of them runs in a container like a worker's: the
same image and limits, the directory being checked mounted at ``/workspace``,
no network (:class:`SandboxChecks`). With ``--sandbox none`` they run on the
host (:class:`HostChecks`).
"""
from __future__ import annotations

import json
import secrets
import shlex
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .cleanup import remove_tree
from .process import ProcessResult, run_process, run_shell
from .sandbox import (
    DEFAULT_IMAGE,
    SandboxConfig,
    check_container_name,
    docker_executor,
    remove_containers,
)
from .workers import write_json_atomic

HIDDEN_TESTS_TIMEOUT_S = 1800.0
HIDDEN_TESTS_DIR = "_hidden_tests"
# Written next to the tests, inside the checked directory, so that a container
# that sees only that directory can write it; then moved to the run directory.
HIDDEN_TESTS_JUNIT = "_hidden_tests.xml"
CHECK_OUTPUT_LIMIT = 200_000

# A shell command line, or an argument list that is run without a shell.
Command = str | Sequence[str]
RunCommand = Callable[[Command], ProcessResult]


class HostChecks:
    """Run check commands on the host (``--sandbox none``)."""

    kind = "none"
    image = None
    # The interpreter for commands the Coordinator builds itself (hidden tests).
    python = sys.executable

    @contextmanager
    def session(self, cwd: Path, timeout: float, label: str) -> Iterator[RunCommand]:
        """Yield a function that runs one command in ``cwd``."""

        def run(command: Command) -> ProcessResult:
            if isinstance(command, str):
                return run_shell(command, cwd=cwd, timeout=timeout)
            return run_process(list(command), cwd=cwd, timeout=timeout)

        yield run


class SandboxChecks:
    """Run check commands in a Docker container that sees only ``cwd``.

    One container serves the commands of one session and is removed at its
    end. ``executor_factory`` has the signature of
    :func:`sandbox.docker_executor`; tests pass a fake.
    """

    kind = "docker"
    python = "python"

    def __init__(self, image: str = DEFAULT_IMAGE, *, executor_factory=docker_executor,
                 remove=remove_containers):
        self.image = image
        self._factory = executor_factory
        self._remove = remove

    @contextmanager
    def session(self, cwd: Path, timeout: float, label: str) -> Iterator[RunCommand]:
        """Yield a function that runs one command in the container, in ``/workspace``.

        The container starts with the first command. A sandbox that cannot
        start or run the command gives a failed result, never an exception:
        a check must not stop the run.
        """
        name = check_container_name(label, secrets.token_hex(4))
        executor = None

        def run(command: Command) -> ProcessResult:
            nonlocal executor
            line = command if isinstance(command, str) else shlex.join(command)
            started = time.monotonic()
            try:
                if executor is None:
                    executor = self._factory(cwd, self.image, name, command_timeout=timeout)
                    executor.start()
                result = executor.execute(line, cwd, timeout)
            except Exception as exc:  # noqa: BLE001 - reported as a failed check
                return ProcessResult(
                    command=line, exit_code=None, timed_out=False,
                    output=f"[sandbox error] {type(exc).__name__}: {exc}\n",
                    duration_s=time.monotonic() - started)
            output = "".join(part if part.endswith("\n") or not part else part + "\n"
                             for part in (result.get("stdout") or "",
                                          result.get("stderr") or ""))
            timed_out = bool(result.get("timed_out"))
            return ProcessResult(
                command=line, exit_code=None if timed_out else result.get("exit_code"),
                output=output, timed_out=timed_out, duration_s=time.monotonic() - started)

        try:
            yield run
        finally:
            if executor is not None:
                try:
                    executor.stop()
                except Exception:  # noqa: BLE001 - remove_containers is the backstop
                    pass
                self._remove(name)


Checks = HostChecks | SandboxChecks


def checks_for(sandbox: SandboxConfig) -> Checks:
    """The checks that go with a worker sandbox: Docker with Docker, host with none."""
    if sandbox.kind == "docker":
        return SandboxChecks(sandbox.image)
    return HostChecks()


@dataclass
class CheckOutcome:
    """Result of running a list of shell commands."""

    ok: bool
    text: str
    duration_s: float
    results: list[dict]


def run_checks(commands: list[str], cwd: Path, timeout: float, *,
               stop_on_failure: bool = True, checks: Checks | None = None,
               label: str = "check") -> CheckOutcome:
    """Run ``commands`` in order through the shell, on the host or in the sandbox."""
    started = time.monotonic()
    parts, results, ok = [], [], True
    with (checks or HostChecks()).session(cwd, timeout, label) as run:
        for command in commands:
            if not command.strip():
                continue
            result = run(command)
            status = "timeout" if result.timed_out else f"exit {result.exit_code}"
            parts.append(f"$ {command}\n[{status}, {result.duration_s:.2f}s]\n{result.output}")
            results.append({"command": command, "ok": result.ok,
                            "exit_code": result.exit_code, "timed_out": result.timed_out,
                            "duration_s": round(result.duration_s, 3)})
            if not result.ok:
                ok = False
                if stop_on_failure:
                    break
    return CheckOutcome(ok=ok, text="\n".join(parts)[-CHECK_OUTPUT_LIMIT:],
                        duration_s=time.monotonic() - started, results=results)


def parse_junit(path: Path) -> dict:
    """Count passed, failed, errors and skipped tests in a JUnit XML file."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        return {**counts, "parse_error": str(exc)}
    suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
    total = 0
    for suite in suites:
        total += int(suite.get("tests", 0))
        counts["failed"] += int(suite.get("failures", 0))
        counts["errors"] += int(suite.get("errors", 0))
        counts["skipped"] += int(suite.get("skipped", 0))
    counts["passed"] = total - counts["failed"] - counts["errors"] - counts["skipped"]
    return counts


def run_hidden_tests(final: Path, tests_dir: Path, checks: Checks, *, xml_path: Path,
                     text_path: Path, timeout: float = HIDDEN_TESTS_TIMEOUT_S) -> dict:
    """Run the hidden tests on the code in ``final``; return the counts.

    ``tests_dir`` is copied to ``final/_hidden_tests`` and left there: the
    caller deletes it. The JUnit report goes to ``xml_path`` and pytest's
    output to ``text_path``.
    """
    target = final / HIDDEN_TESTS_DIR
    remove_tree(target)
    shutil.copytree(tests_dir, target)
    junit = final / HIDDEN_TESTS_JUNIT
    for stale in (junit, xml_path):
        stale.unlink(missing_ok=True)
    with checks.session(final, timeout, "hidden-tests") as run:
        result = run([checks.python, "-m", "pytest", "-q", HIDDEN_TESTS_DIR, "-p",
                      "no:cacheprovider", f"--junitxml={HIDDEN_TESTS_JUNIT}"])
    if junit.is_file():
        shutil.move(junit, xml_path)
    text_path.write_text(result.output, encoding="utf-8")
    stats = parse_junit(xml_path)
    stats.update({"exit_code": result.exit_code, "timed_out": result.timed_out,
                  "duration_s": round(result.duration_s, 3)})
    return stats


def rescore(run_dir: Path, tests_dir: Path, checks: Checks) -> dict:
    """Run the hidden tests again on the ``final/`` of a finished run.

    Writes ``hidden_tests_rescored.txt`` and ``.xml`` and adds
    ``hidden_tests_rescored`` to ``summary.json``; the run's own
    ``hidden_tests`` entry and files are left as they are. The copied tests
    are deleted afterwards; if that fails, the result has ``cleanup_error``.

    Raises ``FileNotFoundError`` when ``final/`` or ``summary.json`` is
    missing and ``ValueError`` when ``summary.json`` is not a JSON object.
    """
    final, summary_path = run_dir / "final", run_dir / "summary.json"
    if not final.is_dir():
        raise FileNotFoundError(f"no final/ directory in {run_dir}")
    if not summary_path.is_file():
        raise FileNotFoundError(f"no summary.json in {run_dir}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError(f"{summary_path} is not a JSON object")
    cleanup_error = ""
    try:
        stats = run_hidden_tests(final, tests_dir, checks,
                                 xml_path=run_dir / "hidden_tests_rescored.xml",
                                 text_path=run_dir / "hidden_tests_rescored.txt")
    finally:
        try:
            remove_tree(final / HIDDEN_TESTS_DIR)
            (final / HIDDEN_TESTS_JUNIT).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - the results are still worth writing
            cleanup_error = f"{type(exc).__name__}: {exc}"
    stats.update({"sandbox": checks.kind, "sandbox_image": checks.image,
                  "hidden_tests": str(tests_dir),
                  "rescored_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if cleanup_error:
        stats["cleanup_error"] = cleanup_error
    summary["hidden_tests_rescored"] = stats
    write_json_atomic(summary_path, summary)
    return stats
