"""Check the hidden tests and the reference solution of the click config task.

    python experiments/taskgraph/click_config/tools/verify.py [--no-docker] [--json OUT] [--work DIR]

No model is called. In a temporary directory (or ``--work``, which is kept)
it builds repositories with ``make_repo.py`` and checks, in order:

1. on the base repository, every functional test (``test_c*.py``) fails;
   the ones that pass are listed (there should be none);
2. on base + reference, every hidden test passes, functional tests and
   ``regression/``: on the host, and in the Docker image
   ``aqours-code-eval:py311`` the way the Coordinator runs them
   (``checks.run_hidden_tests``: the tests are copied into the repository as
   ``_hidden_tests/`` and run with ``python -m pytest -q _hidden_tests``);
3. on base + reference, the repository's own ``tests/`` pass (host and
   Docker);
4. the hidden tests take less than 60 seconds in Docker;
5. spot checks that each section's tests depend only on that section:
   without ``save_config_option`` only ``test_c09`` fails (``test_c01`` and
   ``test_c02`` still pass), and without the help output only ``test_c10``
   fails;
6. ``hidden_tests/regression/``, ``conftest.py`` and ``sandbox_skips.txt``
   are byte for byte those of the click batch task (SHA-256 of every file).

It prints one line per check and exits with 0 when every check passes, 1
otherwise. ``--no-docker`` skips the Docker runs (items 2 to 4 then cover
the host only, and item 4 is not checked).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK.parents[2]
HIDDEN = TASK / "hidden_tests"
CLICK_BATCH_HIDDEN = TASK.parent / "click_batch" / "hidden_tests"
DOCKER_LIMIT_S = 60.0
TIMEOUT_S = 900.0
SHARED = ("regression", "conftest.py", "sandbox_skips.txt")

sys.path.insert(0, str(ROOT))
from aqours_code.taskgraph.checks import (  # noqa: E402
    HostChecks,
    SandboxChecks,
    run_hidden_tests,
)
from aqours_code.taskgraph.cleanup import remove_tree  # noqa: E402


def _load_make_repo():
    spec = importlib.util.spec_from_file_location("click_config_make_repo",
                                                  TASK / "make_repo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_repo = _load_make_repo()

# The help output part of the reference: the configured value in ``--help``.
HELP_BRANCH = '''\
        # A configured value is shown instead of the default, whether or not
        # defaults are shown.
        config_value = self.value_from_config(ctx)

        if config_value is not None:
            config_string = self._format_help_default(config_value)

            if config_string:
                extra["default"] = _("{value} (config)").format(value=config_string)
        elif show_default_is_str or (
'''


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected the text to replace exactly once")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def drop_save_config_option(repo: Path) -> None:
    """Remove ``save_config_option`` and its helpers from the reference."""
    replace_once(repo / "src/click/__init__.py",
                 "from .decorators import save_config_option as save_config_option\n", "")
    path = repo / "src/click/decorators.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("\n\ndef _config_value(")
    text = text[:start] + "\n"
    if "save_config_option" in text:
        raise RuntimeError("save_config_option is still defined")
    path.write_text(text, encoding="utf-8", newline="\n")


def drop_help_output(repo: Path) -> None:
    """Remove the configured value from ``--help`` in the reference."""
    replace_once(repo / "src/click/core.py", HELP_BRANCH, "        if show_default_is_str or (\n")


def junit_results(xml_path: Path) -> dict[str, str]:
    """Each test (``module::name``) and its outcome: passed, failed, error or skipped."""
    outcomes = {"failure": "failed", "error": "error", "skipped": "skipped"}
    results = {}
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        status = "passed"
        for child in case:
            if child.tag in outcomes and status == "passed":
                status = outcomes[child.tag]
        results[f"{case.get('classname', '')}::{case.get('name', '')}"] = status
    return results


def section(test_id: str) -> str | None:
    """``test_c01`` ... ``test_c10`` for a functional test, ``None`` otherwise."""
    match = re.match(r"_hidden_tests\.(test_c\d\d)_", test_id)
    return match.group(1) if match else None


def by_section(results: dict[str, str]) -> dict[str, dict[str, int]]:
    """Per section, the number of tests and of passed tests."""
    counts: dict[str, Counter] = {}
    for test_id, status in results.items():
        name = section(test_id)
        if name is not None:
            counts.setdefault(name, Counter())["total"] += 1
            counts[name]["passed"] += status == "passed"
    return {name: {"total": c["total"], "passed": c["passed"]}
            for name, c in sorted(counts.items())}


def hidden_run(repo: Path, checks, label: str, work: Path) -> dict:
    """Run the hidden tests on ``repo`` as the Coordinator does."""
    started = time.monotonic()
    stats = run_hidden_tests(repo, HIDDEN, checks, xml_path=work / f"{label}.xml",
                             text_path=work / f"{label}.txt", timeout=TIMEOUT_S)
    stats["wall_s"] = round(time.monotonic() - started, 3)
    xml_path = work / f"{label}.xml"
    stats["results"] = junit_results(xml_path) if xml_path.is_file() else {}
    return stats


def public_run(repo: Path, checks, python: str) -> dict:
    """Run ``python -m pytest -q tests`` on ``repo``."""
    with checks.session(repo, TIMEOUT_S, "public-tests") as run:
        result = run([python, "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider"])
    lines = [line for line in result.output.splitlines()
             if re.search(r"\d+ (passed|failed)", line)]
    return {"exit_code": result.exit_code, "timed_out": result.timed_out,
            "duration_s": round(result.duration_s, 3),
            "summary": lines[-1].strip("= ") if lines else result.output[-500:]}


def file_hashes(base: Path) -> dict[str, str]:
    """SHA-256 of every shared file under ``base``, by relative path."""
    hashes = {}
    for name in SHARED:
        path = base / name
        files = [path] if path.is_file() else sorted(path.rglob("*"))
        for file in files:
            if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                rel = file.relative_to(base).as_posix()
                hashes[rel] = hashlib.sha256(file.read_bytes()).hexdigest()
    return hashes


def digest(hashes: dict[str, str]) -> str:
    lines = "".join(f"{rel} {value}\n" for rel, value in sorted(hashes.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def counts_text(stats: dict) -> str:
    return (f"{stats['passed']} passed, {stats['failed']} failed, {stats['errors']} errors, "
            f"{stats['skipped']} skipped")


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, item: str, ok: bool | None, text: str, **data) -> None:
        mark = {True: "ok", False: "FAIL", None: "skip"}[ok]
        print(f"[{mark:4}] {item}: {text}", flush=True)
        self.checks.append({"item": item, "ok": ok, "text": text, **data})

    @property
    def ok(self) -> bool:
        return all(check["ok"] is not False for check in self.checks)


def verify(work: Path, docker: bool) -> Report:
    report = Report()
    host = HostChecks()
    sandbox = SandboxChecks() if docker else None

    base = work / "base"
    commit = make_repo.make_repo(base)
    report.add("0 base commit", commit == make_repo.BASE_COMMIT, commit)

    # 1. Base: no functional test passes.
    stats = hidden_run(base, host, "base-host", work)
    functional = {k: v for k, v in stats["results"].items() if section(k)}
    passing = sorted(k for k, v in functional.items() if v == "passed")
    outcome = Counter(functional.values())
    regression = Counter(v for k, v in stats["results"].items() if not section(k))
    report.add("1 base, functional tests that pass", not passing and bool(functional),
               f"{len(passing)} of {len(functional)} pass ({dict(outcome)}); "
               f"regression/ on the base: {dict(regression)}", passing=passing)

    # 2-4. Reference: hidden tests and public tests, host and Docker.
    ref = work / "reference"
    make_repo.make_repo(ref, with_reference=True)
    stats = hidden_run(ref, host, "reference-host", work)
    sections = by_section(stats["results"])
    report.add("2 reference, hidden tests, host",
               stats["exit_code"] == 0 and stats["failed"] == stats["errors"] == 0,
               counts_text(stats) + f" in {stats['duration_s']} s", sections=sections)
    if sandbox is not None:
        stats = hidden_run(ref, sandbox, "reference-docker", work)
        report.add("2 reference, hidden tests, Docker",
                   stats["exit_code"] == 0 and stats["failed"] == stats["errors"] == 0,
                   counts_text(stats), sections=by_section(stats["results"]))
        report.add("4 hidden tests in Docker take < 60 s",
                   stats["duration_s"] < DOCKER_LIMIT_S,
                   f"{stats['duration_s']} s (run_hidden_tests duration_s; "
                   f"{stats['wall_s']} s wall time)")
    else:
        report.add("2 reference, hidden tests, Docker", None, "--no-docker")
        report.add("4 hidden tests in Docker take < 60 s", None, "--no-docker")

    result = public_run(ref, host, sys.executable)
    report.add("3 reference, tests/, host", result["exit_code"] == 0, result["summary"])
    if sandbox is not None:
        result = public_run(ref, sandbox, sandbox.python)
        report.add("3 reference, tests/, Docker", result["exit_code"] == 0, result["summary"])
    else:
        report.add("3 reference, tests/, Docker", None, "--no-docker")

    # 5. Spot checks: each section depends only on its own part.
    for label, drop, failing in (("no-save-config-option", drop_save_config_option, "test_c09"),
                                 ("no-help-output", drop_help_output, "test_c10")):
        repo = work / label
        make_repo.make_repo(repo, with_reference=True)
        drop(repo)
        sections = by_section(hidden_run(repo, host, label, work)["results"])
        others_pass = all(c["passed"] == c["total"] for name, c in sections.items()
                          if name != failing)
        text = ", ".join(f"{name} {c['passed']}/{c['total']}" for name, c in sections.items())
        report.add(f"5 reference without {label.removeprefix('no-')}: only {failing} fails",
                   sections.get(failing, {}).get("passed", 1) == 0 and others_pass,
                   f"passed per section: {text}", sections=sections)

    # 6. Shared files are those of the click batch task.
    ours, theirs = file_hashes(HIDDEN), file_hashes(CLICK_BATCH_HIDDEN)
    differing = sorted(rel for rel in ours.keys() | theirs.keys()
                       if ours.get(rel) != theirs.get(rel))
    report.add("6 regression/, conftest.py, sandbox_skips.txt equal click_batch's",
               not differing and bool(ours),
               f"{len(ours)} files, SHA-256 of the list {digest(ours)[:16]} "
               f"(click_batch {digest(theirs)[:16]})", differing=differing)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-docker", action="store_true", help="skip the Docker runs")
    parser.add_argument("--json", help="also write the results to this JSON file")
    parser.add_argument("--work", help="work in this (empty) directory and keep it")
    args = parser.parse_args(argv)
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="click-config-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        report = verify(work, docker=not args.no_docker)
    finally:
        if not args.work:
            remove_tree(work)
    if args.json:
        Path(args.json).write_text(json.dumps(report.checks, indent=2) + "\n",
                                   encoding="utf-8")
    print("all checks passed" if report.ok else "some checks FAILED")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
