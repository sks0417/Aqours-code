"""SWE-EVO instances: grade the final code of a run with the instance's tests.

A SWE-EVO instance (https://github.com/SWE-EVO/SWE-EVO) is one release of a
Python project: the repository at the old version, the release notes as the
request, and SWE-bench-style test ids. ``FAIL_TO_PASS`` (F2P) tests fail
before the release and pass after it; ``PASS_TO_PASS`` (P2P) tests pass both
times. The grading data lives in a task directory that workers never see
(``experiments/taskgraph/sweevo/prepare.py`` writes it)::

    <task>/repo/                   the repository, one commit (the base)
    <task>/request.md              the request
    <task>/sandbox.json            image and environment of the instance
    <task>/sandbox_skips.txt       tests left out of every score (optional)
    <task>/grading/instance.json   ids, test command, log parser, F2P, P2P
    <task>/grading/test.patch      the release's test changes

Grading copies the code to a scratch directory, puts the files the test
patch touches back to the base version and applies the patch (as SWE-bench
does), runs the instance's test command on the test files of the patch in the
instance image, and reads the statuses from pytest's ``-rA`` summary with the
instance's own log parser. A test passes when its status is ``PASSED`` or
``XFAIL``, fails when it is ``FAILED`` or ``ERROR`` or missing from the log,
and counts neither way when it is ``SKIPPED`` (SWE-bench's rules).

This module calls git only through ``gitops``.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import gitops
from .checks import Checks, SandboxChecks
from .cleanup import remove_tree
from .sandbox import SandboxConfig, load_sandbox_config
from .workers import write_json_atomic

INSTANCE_FILE = Path("grading") / "instance.json"
TEST_PATCH_FILE = Path("grading") / "test.patch"
SANDBOX_FILE = "sandbox.json"
SKIPS_FILE = "sandbox_skips.txt"
REPO_DIR = "repo"
EVAL_TIMEOUT_S = 3600.0
SUMMARY_KEY = "sweevo"
RESULTS_FILE = "sweevo_results.json"
LOG_FILE = "sweevo_tests.txt"
SCRATCH_DIR = "sweevo_eval"
KINDS = ("FAIL_TO_PASS", "PASS_TO_PASS")
SHORT = {"FAIL_TO_PASS": "f2p", "PASS_TO_PASS": "p2p"}
STATUSES = ("FAILED", "PASSED", "SKIPPED", "ERROR", "XFAIL")
PASSING = ("PASSED", "XFAIL")
FAILING = ("FAILED", "ERROR")
MISSING = "MISSING"
EXCLUDED = "EXCLUDED"
# SWE-bench leaves files with these endings out of the test command.
NON_TEST_EXTS = (".json", ".png", "csv", ".txt", ".md", ".jpg", ".jpeg", ".pkl",
                 ".yml", ".yaml", ".toml")
# Run before the tests: git (and libgit2) trust /workspace, which belongs to another user.
GIT_SETUP = ('mkdir -p "${HOME:-/tmp/home}" && git config --global --add safe.directory "*"'
             " >/dev/null 2>&1; ")
# What SWE-EVO's harness adds to every test command (its make_eval_script_list_py).
EXTRA_PYTEST_OPTIONS = ("pytest --continue-on-collection-errors -W default::DeprecationWarning "
                        "-W default::pytest.PytestDeprecationWarning")


class SweevoError(Exception):
    """A task directory or a run cannot be graded."""


# ── log parsers (SWE-EVO's, unchanged in behaviour) ──

def parse_log_pytest(log: str) -> dict[str, str]:
    """Statuses from pytest's ``-rA`` summary lines (SWE-bench's ``parse_log_pytest``).

    ``PASSED a.py::t`` and ``FAILED a.py::t - message``; a test id is the
    first word after the status, so an id with a space is cut there, as it is
    in the dataset.
    """
    status_map: dict[str, str] = {}
    for line in log.split("\n"):
        if any(line.startswith(status) for status in STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            words = line.split()
            if len(words) <= 1:
                continue
            status_map[words[1]] = words[0]
    return status_map


_ANSI_CODE = re.compile(r"\[(\d+)m")
_CONTROL = str.maketrans("", "", "".join(chr(code) for code in range(1, 32)))
_FAILED_PERCENT = re.compile(r"FAILED\s*\[.*?\]")


def parse_log_pytest_pydantic(log: str) -> dict[str, str]:
    """SWE-EVO's parser for pydantic: colour codes removed, ``-vv`` lines too.

    Besides the summary lines, ``tests/a.py::t PASSED`` (a verbose line that
    ends with its status) counts, and ``FAILED [ 10%]`` loses the percentage.
    """
    status_map: dict[str, str] = {}
    for line in log.split("\n"):
        line = _ANSI_CODE.sub("", line).translate(_CONTROL)
        line = _FAILED_PERCENT.sub("FAILED", line)
        if any(line.startswith(status) for status in STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            words = line.split()
            if len(words) > 1:
                status_map[words[1]] = words[0]
        elif any(line.endswith(status) for status in STATUSES):
            words = line.split()
            if len(words) > 1:
                status_map[words[0]] = words[1]
    return status_map


LOG_PARSERS = {
    "parse_log_pytest": parse_log_pytest,
    "parse_log_pytest_pydantic": parse_log_pytest_pydantic,
}


# ── the test command ──

_DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.M)


def patch_files(patch: str) -> list[dict]:
    """Each file of a git diff: ``old``, ``new`` path and whether it is deleted or created."""
    headers = list(_DIFF_HEADER.finditer(patch))
    files = []
    for number, header in enumerate(headers):
        end = headers[number + 1].start() if number + 1 < len(headers) else len(patch)
        block = patch[header.end():end]
        files.append({
            "old": header.group(1), "new": header.group(2),
            "deleted": bool(re.search(r"(?m)^deleted file mode \d+", block)
                            or re.search(r"(?m)^\+\+\+ /dev/null", block)),
            "created": bool(re.search(r"(?m)^new file mode \d+", block)
                            or re.search(r"(?m)^--- /dev/null", block)),
        })
    return files


def test_directives(test_patch: str, repo: str) -> list[str]:
    """The test files the test command runs: those of the test patch (SWE-EVO's rules).

    Files ending in a non-test extension and files the patch deletes are
    left out; for dask, only ``.py`` files are kept.
    """
    directives = []
    for item in patch_files(test_patch):
        path = item["new"]
        if item["deleted"] or any(path.endswith(ext) for ext in NON_TEST_EXTS):
            continue
        if repo == "dask/dask" and not path.endswith(".py"):
            continue
        if path not in directives:
            directives.append(path)
    return directives


def test_command(test_cmd: str, directives: Iterable[str],
                 per_test_timeout: int | None = None) -> str:
    """The instance's test command with SWE-EVO's extra pytest options and the files.

    ``per_test_timeout`` adds ``--timeout`` (the pytest-timeout plugin): a
    test that hangs in our sandbox then fails alone instead of stopping the
    whole run at the command's own timeout.
    """
    extra = EXTRA_PYTEST_OPTIONS
    if per_test_timeout is not None:
        extra += f" --timeout={per_test_timeout}"
    command = test_cmd.replace("pytest", extra, 1)
    return " ".join([command.strip(), *directives])


# ── grading ──

def outcome(test_id: str, status_map: Mapping[str, str]) -> str:
    """``PASSED``/``XFAIL``/``FAILED``/``ERROR``/``SKIPPED``, or ``MISSING`` if not in the log."""
    return status_map.get(test_id, MISSING)


def count(test_ids: Iterable[str], status_map: Mapping[str, str],
          excluded: Iterable[str]) -> dict:
    """Passed, failed, errors, missing, skipped and excluded tests of one list.

    ``total`` is the listed tests less the excluded ones; ``rate`` is passed
    / (passed + failed + errors + missing), 1.0 when that is 0.
    """
    excluded = set(excluded)
    counts = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "missing": 0,
              "skipped": 0, "excluded": 0}
    for test_id in test_ids:
        if test_id in excluded:
            counts["excluded"] += 1
            continue
        counts["total"] += 1
        status = outcome(test_id, status_map)
        if status in PASSING:
            counts["passed"] += 1
        elif status == "FAILED":
            counts["failed"] += 1
        elif status == "ERROR":
            counts["errors"] += 1
        elif status == MISSING:
            counts["missing"] += 1
        else:
            counts["skipped"] += 1
    counted = counts["passed"] + counts["failed"] + counts["errors"] + counts["missing"]
    counts["rate"] = counts["passed"] / counted if counted else 1.0
    return counts


def grade(status_map: Mapping[str, str], fail_to_pass: Iterable[str],
          pass_to_pass: Iterable[str], excluded: Iterable[str] = ()) -> dict:
    """F2P and P2P counts, ``resolved``, SWE-EVO's ``fix_rate``, and ``q``.

    ``resolved``: every counted F2P and P2P test passes. ``fix_rate``: the
    F2P pass rate if no P2P test fails, else 0. ``q`` = F2P rate × P2P rate.
    """
    excluded = list(excluded)
    f2p = count(fail_to_pass, status_map, excluded)
    p2p = count(pass_to_pass, status_map, excluded)
    p2p_ok = p2p["passed"] + p2p["skipped"] == p2p["total"]
    f2p_ok = f2p["passed"] + f2p["skipped"] == f2p["total"]
    return {"f2p": f2p, "p2p": p2p, "resolved": f2p_ok and p2p_ok,
            "fix_rate": f2p["rate"] if p2p_ok else 0.0, "q": f2p["rate"] * p2p["rate"]}


def per_test(status_map: Mapping[str, str], fail_to_pass: Iterable[str],
             pass_to_pass: Iterable[str], excluded: Iterable[str] = ()) -> dict:
    """``{"FAIL_TO_PASS": {id: status}, "PASS_TO_PASS": {...}}``; excluded tests say so."""
    excluded = set(excluded)
    return {kind: {test_id: EXCLUDED if test_id in excluded else outcome(test_id, status_map)
                   for test_id in ids}
            for kind, ids in zip(KINDS, (fail_to_pass, pass_to_pass))}


# ── the task directory ──

def load_skips(path: Path) -> list[str]:
    """Test ids, one per line; ``#`` starts a comment line. A missing file is an empty list."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]



def _id_list(value, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SweevoError(f"{name} must be a list of test ids")
    return tuple(value)


@dataclass(frozen=True)
class SweevoTask:
    """What grading needs from one prepared instance."""

    task_dir: Path
    instance_id: str
    repo: str
    base_commit: str  # the upstream commit; the task repository has one commit of its tree
    test_cmd: str
    log_parser: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    test_patch: str
    sandbox: SandboxConfig
    exclusions: tuple[str, ...] = ()
    test_timeout: int | None = None  # seconds per test (pytest-timeout), if the image has it

    @property
    def repo_dir(self) -> Path:
        """The task repository; its ``HEAD`` is the base the test patch applies to."""
        return self.task_dir / REPO_DIR

    @property
    def directives(self) -> list[str]:
        """The test files the test command runs."""
        return test_directives(self.test_patch, self.repo)

    @property
    def command(self) -> str:
        """The shell command that runs the tests in the container."""
        return test_command(self.test_cmd, self.directives, self.test_timeout)

    def parse(self, log: str) -> dict[str, str]:
        """Test statuses from the test output, with the instance's log parser."""
        return LOG_PARSERS[self.log_parser](log)


def load_task(task_dir: Path, exclusions: Path | None = None) -> SweevoTask:
    """Read a prepared task directory; ``exclusions`` replaces ``<task>/sandbox_skips.txt``."""
    task_dir = Path(task_dir).resolve()
    instance_path = task_dir / INSTANCE_FILE
    try:
        data = json.loads(instance_path.read_text(encoding="utf-8"))
        test_patch = (task_dir / TEST_PATCH_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise SweevoError(f"{task_dir} is not a prepared SWE-EVO task: {exc}") from None
    if not isinstance(data, dict):
        raise SweevoError(f"{instance_path} is not a JSON object")
    for key in ("instance_id", "repo", "base_commit", "test_cmds", "log_parser"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise SweevoError(f"{instance_path} needs a non-empty \"{key}\"")
    if data["log_parser"] not in LOG_PARSERS:
        raise SweevoError(f"unknown log parser {data['log_parser']!r} "
                          f"(known: {', '.join(LOG_PARSERS)})")
    if not (task_dir / REPO_DIR).is_dir():
        raise SweevoError(f"no repository in {task_dir / REPO_DIR}")
    try:
        sandbox = load_sandbox_config(task_dir / SANDBOX_FILE)
    except (OSError, ValueError) as exc:
        raise SweevoError(f"cannot read {task_dir / SANDBOX_FILE}: {exc}") from None
    skips = load_skips(exclusions if exclusions is not None else task_dir / SKIPS_FILE)
    timeout = data.get("test_timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                or timeout <= 0):
        raise SweevoError(f"{instance_path}: \"test_timeout\" must be a positive integer")
    return SweevoTask(
        task_dir=task_dir, instance_id=data["instance_id"], repo=data["repo"],
        base_commit=data["base_commit"], test_cmd=data["test_cmds"],
        log_parser=data["log_parser"],
        fail_to_pass=_id_list(data.get("FAIL_TO_PASS"), "FAIL_TO_PASS"),
        pass_to_pass=_id_list(data.get("PASS_TO_PASS"), "PASS_TO_PASS"),
        test_patch=test_patch, sandbox=sandbox, exclusions=tuple(skips),
        test_timeout=timeout)


# ── running the tests ──

def _copy_code(source: Path, dest: Path) -> None:
    """Copy ``source`` to ``dest`` without ``.git`` and Python caches."""
    shutil.copytree(source, dest, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))


def stage_code(source: Path | None, dest: Path, task: SweevoTask, *,
               commit: tuple[Path, str] | None = None, patches: Iterable[str] = ()) -> None:
    """Write the code to grade to ``dest``, with the release's tests on top.

    The code is ``source`` copied (without ``.git``), or ``commit`` =
    ``(repo, sha)`` exported from a repository. ``patches`` are applied
    first (validation applies the gold patch this way). Then every file the
    test patch touches is put back to its base version (removed if the base
    has no such file), so that an edit of a test file cannot stop the patch
    from applying, and the test patch is applied. Last, ``dest`` becomes a
    git repository whose ``HEAD`` is the task's base commit, with all of
    this as uncommitted changes: SWE-bench's ``/testbed`` is such a
    repository, and some tests need one (dvc's ``Repo()`` in the current
    directory).
    """
    remove_tree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if commit is not None:
        gitops.export_commit(commit[0], commit[1], dest)
    elif source is not None:
        _copy_code(source, dest)
    else:
        raise ValueError("stage_code needs a source directory or a commit")
    for patch in patches:
        gitops.apply_patch(dest, patch)
    for item in patch_files(task.test_patch):
        for path in dict.fromkeys((item["old"], item["new"])):
            target = dest / path
            content = gitops.blob_at(task.repo_dir, "HEAD", path)
            if content is None:
                if target.is_file() or target.is_symlink():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    gitops.apply_patch(dest, task.test_patch)
    gitops.attach_repository(dest, task.repo_dir)


def task_checks(task: SweevoTask) -> SandboxChecks:
    """Checks in the instance's image, with its environment."""
    if task.sandbox.kind != "docker":
        raise SweevoError("SWE-EVO tests run only in the instance's Docker image")
    return SandboxChecks(task.sandbox.image, env=task.sandbox.env, nofile=task.sandbox.nofile,
                         pids=task.sandbox.pids, git_view=task.sandbox.git_view)


def run_tests(code: Path, task: SweevoTask, checks: Checks, *,
              timeout: float = EVAL_TIMEOUT_S) -> dict:
    """Run the instance's tests on ``code`` (mounted at ``/workspace``); return the raw result.

    First, as SWE-EVO's eval script does for its non-root user, git is told
    to trust the repository: the mounted files belong to another user.
    """
    with checks.session(code, timeout, f"sweevo-{task.instance_id}") as run:
        result = run(GIT_SETUP + task.command)
    return {"command": task.command, "exit_code": result.exit_code,
            "timed_out": result.timed_out, "duration_s": round(result.duration_s, 3),
            "output": result.output,
            "sandbox_error": result.output.startswith("[sandbox error]")}


def evaluate(code: Path, task: SweevoTask, checks: Checks, *, log_path: Path,
             timeout: float = EVAL_TIMEOUT_S) -> tuple[dict, dict]:
    """Run and grade the tests on the staged ``code``; write the output to ``log_path``.

    Returns the summary entry (counts, ``resolved``, ``fix_rate``, ``q``,
    how the tests ran) and the status of every listed test.
    """
    raw = run_tests(code, task, checks, timeout=timeout)
    log_path.write_text(f"$ {raw['command']}\n{raw['output']}", encoding="utf-8")
    status_map = task.parse(raw["output"])
    graded = grade(status_map, task.fail_to_pass, task.pass_to_pass, task.exclusions)
    ok = not raw["timed_out"] and not raw["sandbox_error"]
    error = ""
    if raw["sandbox_error"]:
        error = raw["output"].strip().splitlines()[0][:500]
    elif raw["timed_out"]:
        error = f"the tests timed out after {timeout:.0f} s"
    entry = {
        "instance_id": task.instance_id, "ok": ok, "error": error, **graded,
        "excluded_tests": len(task.exclusions), "tests_in_log": len(status_map),
        "exit_code": raw["exit_code"], "timed_out": raw["timed_out"],
        "duration_s": raw["duration_s"], "command": raw["command"],
        "sandbox_image": getattr(checks, "image", None),
        "sandbox_env": dict(getattr(checks, "env", ()) or ()),
        "sandbox_nofile": getattr(checks, "nofile", None),
        "sandbox_pids": getattr(checks, "pids", None),
    }
    return entry, per_test(status_map, task.fail_to_pass, task.pass_to_pass, task.exclusions)


def evaluate_run(run_dir: Path, task: SweevoTask, checks: Checks | None = None, *,
                 timeout: float = EVAL_TIMEOUT_S, keep_scratch: bool = False) -> dict:
    """Grade the final code of a finished run; record it in the run directory.

    The code is ``final/`` when the run has one, else its integration commit
    exported from ``repo/``. Writes ``sweevo_tests.txt`` (the test output),
    ``sweevo_results.json`` (every listed test's status) and the ``sweevo``
    entry of ``summary.json``; a second evaluation replaces the first.
    ``sweevo_eval/``, the scratch copy, is deleted unless ``keep_scratch``.
    """
    run_dir = Path(run_dir).resolve()
    summary_path = run_dir / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SweevoError(f"cannot read {summary_path}: {exc}") from None
    if not isinstance(summary, dict):
        raise SweevoError(f"{summary_path} is not a JSON object")
    checks = checks or task_checks(task)
    scratch = run_dir / SCRATCH_DIR
    started = time.monotonic()
    final = run_dir / "final"
    if final.is_dir():
        source, commit, origin = final, None, "final"
    else:
        sha = summary.get("integration_commit")
        if not isinstance(sha, str) or not (run_dir / "repo").is_dir():
            raise SweevoError(f"{run_dir} has neither final/ nor repo/ with an "
                              "integration_commit")
        source, commit, origin = None, (run_dir / "repo", sha), "integration_commit"
    try:
        stage_code(source, scratch, task, commit=commit)
        entry, tests = evaluate(scratch, task, checks, log_path=run_dir / LOG_FILE,
                                timeout=timeout)
    finally:
        if not keep_scratch:
            remove_tree(scratch)
    entry.update({"code": origin, "task_dir": str(task.task_dir),
                  "total_duration_s": round(time.monotonic() - started, 3),
                  "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    write_json_atomic(run_dir / RESULTS_FILE, {**entry, "tests": tests,
                                               "exclusions": list(task.exclusions)})
    summary[SUMMARY_KEY] = entry
    write_json_atomic(summary_path, summary)
    return entry
