"""Make the cleaned ("v2") SWE-EVO tasks.

    python experiments/taskgraph/sweevo/clean_tasks.py <instance_id> [<instance_id> ...]
        [--tasks C:\\tg\\sweevo-tasks] [--out C:\\tg\\sweevo-tasks-v2] [--force]
        [--no-docker] [--validate]

An original task (``prepare.py``) gives the agent the repository before the
release with the old tests. The release's test changes (``grading/test.patch``)
are applied only when grading, so some old tests assert the very behaviour the
release changes and fail when the agent gets it right; and some tests fail on
the unchanged base because the image's packages are newer than the project
expects. Both make an agent investigate without end. A v2 task removes the
first kind and names the second in the request. The original task directory,
its grading and its runs are not touched.

``<out>/<id>/`` has the files of ``<tasks>/<id>/``, with:

- ``repo/``: the original repository (its first commit is the base,
  ``task_commit``) plus a second commit, "remove tests the release rewrites":
  every existing test function or method (parametrized ones included) that
  the test patch changes or deletes is removed from its file, and a test file
  the patch deletes is removed. Tests the patch adds do not exist yet; other
  tests of the same file are kept. Every changed file still parses, and the
  tests of the patch's files collect in the task image: exactly the base's
  tests less the removed ones (``--no-docker`` skips that check).
- ``cleaning.json``: what was removed, from which file, and the hunks of the
  patch that touch it.
- ``known_failing.txt``: the tests that fail or error when the whole test
  suite runs on the base (``task_commit``) in the task image, with the task's
  test command and ``sandbox.json`` (no model); ``known_failing.json`` has the
  counts and ``known_failing.log`` the output. A removed test is not listed.
- ``request_iface_v2.md``: ``request_iface.md`` unchanged, then the section
  "Tests that already fail before any change" with those tests (the first 60
  if there are more). It is built from the test run alone and is checked to
  contain nothing of the test patch.
- ``single.json`` (``request.md``) and ``single_iface.json``
  (``request_iface_v2.md``): the single-agent graphs at the v2 ``HEAD``,
  with the original's checks.

``grading/`` is copied unchanged; grading restores the patch's test files from
``task_commit`` (``sweevo.stage_code``), so a v2 run is graded exactly like a
v1 run. ``--validate`` then grades the base and the gold patch of both
versions (as ``validate_gold.py`` does, with nothing written to its
exclusion lists or reports) and writes ``validation.json``: on the v2 base
every F2P test must fail, with the gold patch every F2P test must pass, and
every test's status must be the one of v1.
"""
from __future__ import annotations

import argparse
import ast
import configparser
import dataclasses
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # the Aqours checkout, for aqours_code.taskgraph
sys.path.insert(0, str(HERE))

from aqours_code.taskgraph import gitops  # noqa: E402
from aqours_code.taskgraph.cleanup import remove_tree  # noqa: E402
from aqours_code.taskgraph.schema import dump_graph, load_graph  # noqa: E402
from aqours_code.taskgraph.single import single_graph  # noqa: E402
from aqours_code.taskgraph.sweevo import (  # noqa: E402
    FAILING, GIT_SETUP, KINDS, SweevoTask, evaluate_run, load_task, task_checks, test_command)
from prepare import GIT_ENV, REPOS  # noqa: E402

DEFAULT_TASKS = Path(r"C:\tg\sweevo-tasks")
DEFAULT_OUT = Path(r"C:\tg\sweevo-tasks-v2")
COMMIT_SUBJECT = "remove tests the release rewrites"
# the second commit: the same author as the base, a fixed later date
CLEAN_GIT_ENV = {**GIT_ENV, "GIT_AUTHOR_DATE": "2026-01-02T00:00:00+00:00",
                 "GIT_COMMITTER_DATE": "2026-01-02T00:00:00+00:00"}
SECTION_TITLE = "## Tests that already fail before any change"
LIST_LIMIT = 60
KNOWN_FAILING = "known_failing.txt"
CLEANING = "cleaning.json"
REQUEST_V2 = "request_iface_v2.md"
SUITE_TIMEOUT_S = 7200.0
COLLECT_TIMEOUT_S = 900.0
# pytest's defaults, unless the repository's configuration sets python_files
DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")


class CleanError(RuntimeError):
    """A task cannot be cleaned as the rules say."""


# ── the test patch ──

@dataclass
class Hunk:
    """One ``@@`` hunk, as old-file line numbers (1-based)."""

    index: int  # 1-based, within its file
    header: str
    removed: list[int] = field(default_factory=list)
    # (after old line k, the added lines); k = 0 is before the first line
    inserted: list[tuple[int, list[str]]] = field(default_factory=list)


@dataclass
class FileDiff:
    """One file of a git diff."""

    old: str
    new: str
    created: bool
    deleted: bool
    binary: bool
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def renamed(self) -> bool:
        return not self.created and not self.deleted and self.old != self.new


_DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.M)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$")


def parse_patch(patch: str) -> list[FileDiff]:
    """The files and hunks of a git diff (``git apply``'s format)."""
    headers = list(_DIFF_HEADER.finditer(patch))
    files = []
    for number, header in enumerate(headers):
        end = headers[number + 1].start() if number + 1 < len(headers) else len(patch)
        lines = patch[header.end():end].split("\n")[1:]
        diff = FileDiff(old=header.group(1), new=header.group(2), created=False,
                        deleted=False, binary=False)
        position = 0
        while position < len(lines) and not lines[position].startswith("@@"):
            line = lines[position]
            if line.startswith("new file mode") or line == "--- /dev/null":
                diff.created = True
            elif line.startswith("deleted file mode") or line == "+++ /dev/null":
                diff.deleted = True
            elif line.startswith(("GIT binary patch", "Binary files ")):
                diff.binary = True
            position += 1
        while position < len(lines):
            match = _HUNK.match(lines[position])
            if not match:
                position += 1
                continue
            hunk = Hunk(index=len(diff.hunks) + 1, header=lines[position])
            old = int(match.group(1))
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            if old_left == 0:  # "-k,0": the hunk inserts after old line k
                old += 1
            position += 1
            pending: list[str] = []
            while position < len(lines) and (old_left > 0 or new_left > 0):
                line = lines[position]
                position += 1
                if line.startswith("\\"):  # "\ No newline at end of file"
                    continue
                if line.startswith("+"):
                    pending.append(line[1:])
                    new_left -= 1
                    continue
                if pending:
                    hunk.inserted.append((old - 1, pending))
                    pending = []
                if line.startswith("-"):
                    hunk.removed.append(old)
                    old_left -= 1
                else:  # context; a blank context line may have lost its space
                    old_left -= 1
                    new_left -= 1
                old += 1
            if pending:
                hunk.inserted.append((old - 1, pending))
            diff.hunks.append(hunk)
        files.append(diff)
    return files


def defined_test_names(patch: str) -> set[str]:
    """Names of test functions and classes that lines the patch adds define."""
    names = set()
    for diff in parse_patch(patch):
        for hunk in diff.hunks:
            for _, lines in hunk.inserted:
                for line in lines:
                    match = re.match(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)", line)
                    if match and match.group(1).lower().startswith("test"):
                        names.add(match.group(1))
    return names


# ── test definitions in a file ──

@dataclass
class Definition:
    """A test function or method, or a test class, of one file."""

    qualname: str  # as in a pytest id: "test_x", "TestA::test_y"
    kind: str      # "function", "method" or "class"
    start: int     # its first line: the first decorator, else the def/class line
    header_end: int  # the last line of the def/class statement's header
    end: int
    col: int
    parametrized: bool = False
    children: list["Definition"] = field(default_factory=list)
    body: list[tuple[int, int]] = field(default_factory=list)  # spans of a class's statements


def _span(node: ast.AST) -> tuple[int, int]:
    decorators = getattr(node, "decorator_list", [])
    start = min([node.lineno, *(item.lineno for item in decorators)])
    return start, node.end_lineno


def _parametrized(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        text = ast.unparse(decorator)
        if ".parametrize(" in text or text.endswith(".parametrize"):
            return True
    return False


def _definitions(body: list[ast.stmt], prefix: str, in_class: bool) -> list[Definition]:
    found = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test"):
            start, end = _span(node)
            header_end = node.body[0].lineno - 1 if node.body else node.lineno
            found.append(Definition(prefix + node.name, "method" if in_class else "function",
                                    start, max(node.lineno, header_end), end, node.col_offset,
                                    _parametrized(node)))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            start, end = _span(node)
            header_end = node.body[0].lineno - 1 if node.body else node.lineno
            item = Definition(prefix + node.name, "class", start, max(node.lineno, header_end),
                              end, node.col_offset, _parametrized(node))
            item.children = _definitions(node.body, f"{prefix}{node.name}::", True)
            item.body = [_span(child) for child in node.body]
            found.append(item)
    return found


def test_definitions(source: str) -> list[Definition]:
    """The test functions and test classes (with their test methods) of a module."""
    return _definitions(ast.parse(source).body, "", False)


def _walk(items: list[Definition]):
    for item in items:
        yield item
        yield from _walk(item.children)


def _first_code(lines: list[str]) -> str | None:
    for line in lines:
        if line.strip() and not line.strip().startswith("#"):
            return line
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def touched_tests(definitions: list[Definition],
                  hunk: Hunk) -> list[tuple[Definition, list[int]]]:
    """The test functions and methods a hunk changes, each with the old lines that show it.

    A removed line inside a test's span (decorators included) changes it.
    Added lines change it when they go strictly inside its span, or right
    after its last line indented deeper than its ``def``, or right before it
    ending with a decorator. A changed line in the header or decorators of a
    test class changes all its tests. Lines of a class's other members, and
    lines between definitions, change no test.
    """
    hits: dict[str, tuple[Definition, list[int]]] = {}

    def hit(item: Definition, line: int) -> None:
        targets = [child for child in _walk(item.children) if child.kind != "class"] \
            if item.kind == "class" else [item]
        for target in targets:
            hits.setdefault(target.qualname, (target, []))[1].append(line)

    def innermost(line: int) -> Definition | None:
        found = None
        for item in _walk(definitions):
            if item.start <= line <= item.end:
                found = item  # _walk goes outer to inner
        return found

    for line in hunk.removed:
        item = innermost(line)
        if item is None:
            continue
        if item.kind != "class" or line <= item.header_end:
            hit(item, line)
    for after, added in hunk.inserted:
        code = _first_code(added)
        for item in _walk(definitions):
            inside = item.start <= after < item.end
            continuing = (after == item.end and code is not None and
                          _indent(code) > item.col)
            decorating = (after + 1 == item.start and code is not None and
                          (_first_code(list(reversed(added))) or "").strip().startswith("@"))
            if item.kind == "class":
                if decorating or (item.start <= after < item.header_end):
                    hit(item, after)
                continue
            if inside or continuing or decorating:
                hit(item, after)
    return list(hits.values())


# ── removing them ──

def remove_definitions(source: str, removed: list[Definition],
                       definitions: list[Definition]) -> tuple[str, list[str]]:
    """``source`` without ``removed``; also returns test classes left empty and removed.

    Each definition goes from its first decorator to its last line, with the
    blank lines just before it, so the spacing around it stays as it was. A
    test class whose every statement is removed (a docstring and ``pass``
    aside) is removed whole.
    """
    lines = source.splitlines(keepends=True)
    names = {item.qualname for item in removed}
    spans = [(item.start, item.end) for item in removed]
    emptied = []
    for item in reversed(list(_walk(definitions))):  # inner classes first
        if item.kind != "class":
            continue
        gone = {(child.start, child.end) for child in _walk(item.children)
                if child.qualname in names}
        remaining = [span for span in item.body
                     if not any(start <= span[0] and span[1] <= end for start, end in gone)
                     and not _is_filler(lines, span)]
        if gone and not remaining:
            spans.append((item.start, item.end))
            names.add(item.qualname)
            emptied.append(item.qualname)
    drop = set()
    for start, end in spans:
        drop.update(range(start, end + 1))
        before = start - 1
        while before >= 1 and not lines[before - 1].strip() and before not in drop:
            drop.add(before)
            before -= 1
    kept = [line for number, line in enumerate(lines, start=1) if number not in drop]
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    return text, emptied


def _is_filler(lines: list[str], span: tuple[int, int]) -> bool:
    """A docstring or ``pass``: what a class body may keep and still be empty."""
    text = "".join(lines[span[0] - 1:span[1]]).strip()
    if text == "pass":
        return True
    try:
        node = ast.parse(text).body
    except SyntaxError:
        return False
    return (len(node) == 1 and isinstance(node[0], ast.Expr)
            and isinstance(node[0].value, ast.Constant) and isinstance(node[0].value.value, str))


# ── which files hold tests ──

def python_files(repo: Path, commit: str) -> tuple[str, ...]:
    """pytest's ``python_files`` patterns of the repository at ``commit``."""
    for name, section in (("pytest.ini", "pytest"), ("pyproject.toml", None),
                          ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        content = gitops.blob_at(repo, commit, name)
        if content is None:
            continue
        text = content.decode("utf-8", errors="replace")
        if section is None:
            try:
                options = tomllib.loads(text).get("tool", {}).get("pytest", {}).get(
                    "ini_options", {})
            except tomllib.TOMLDecodeError:
                continue
            value = options.get("python_files")
            if value:
                return tuple(value.split() if isinstance(value, str) else value)
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error:
            continue
        if parser.has_option(section, "python_files"):
            return tuple(parser.get(section, "python_files").split())
    return DEFAULT_PYTHON_FILES


def is_test_module(path: str, patterns: tuple[str, ...]) -> bool:
    return path.endswith(".py") and any(fnmatch.fnmatch(PurePosixPath(path).name, pattern)
                                        for pattern in patterns)


# ── the cleaning ──

def clean_plan(repo: Path, commit: str, test_patch: str) -> dict:
    """What the cleaning removes, file by file; nothing is written.

    ``{"files": {path: new text}, "deleted": [paths], "removed": [...],
    "deleted_files": [...], "untouched": [...]}``.
    """
    patterns = python_files(repo, commit)
    plan = {"python_files": list(patterns), "files": {}, "deleted": [], "removed": [],
            "removed_classes": [], "deleted_files": [], "untouched": []}
    for diff in parse_patch(test_patch):
        if diff.created:
            plan["untouched"].append({"file": diff.new, "why": "the patch creates it"})
            continue
        if not is_test_module(diff.old, patterns):
            plan["untouched"].append({"file": diff.old, "why": "not a test module "
                                      f"(python_files: {' '.join(patterns)})"})
            continue
        content = gitops.blob_at(repo, commit, diff.old)
        if content is None:
            raise CleanError(f"{diff.old} is changed by the patch but not in the base")
        source = content.decode("utf-8")
        definitions = test_definitions(source)
        tests = [item for item in _walk(definitions) if item.kind != "class"]
        if diff.deleted or diff.renamed:
            why = "the patch deletes the file" if diff.deleted else \
                f"the patch renames it to {diff.new}"
            plan["deleted"].append(diff.old)
            plan["deleted_files"].append({
                "file": diff.old, "why": why,
                "tests": [item.qualname for item in tests],
                "hunks": [hunk.header for hunk in diff.hunks]})
            continue
        found: dict[str, dict] = {}
        for hunk in diff.hunks:
            for item, lines in touched_tests(definitions, hunk):
                entry = found.setdefault(item.qualname, {
                    "file": diff.old, "test": item.qualname, "kind": item.kind,
                    "parametrized": item.parametrized, "lines": [item.start, item.end],
                    "why": "changed", "hunks": [], "_item": item})
                entry["hunks"].append({"index": hunk.index, "header": hunk.header,
                                       "old_lines": sorted(set(lines))})
        if not found:
            plan["untouched"].append({"file": diff.old, "why": "the patch changes no existing "
                                      "test in it (it only adds tests or changes other code)"})
            continue
        removed_lines = {line for hunk in diff.hunks for line in hunk.removed}
        for entry in found.values():
            item = entry["_item"]
            if all(line in removed_lines for line in range(item.start, item.end + 1)):
                entry["why"] = "deleted"  # every line of it is removed by the patch
        text, emptied = remove_definitions(source, [entry["_item"] for entry in found.values()],
                                           definitions)
        try:
            ast.parse(text)
        except SyntaxError as exc:
            raise CleanError(f"{diff.old} does not parse after the cleaning: {exc}") from None
        plan["files"][diff.old] = text
        for entry in found.values():
            entry.pop("_item")
            plan["removed"].append(entry)
        plan["removed_classes"] += [{"file": diff.old, "class": name,
                                     "why": "all its tests are removed"} for name in emptied]
    return plan


def removed_ids(plan: dict) -> list[str]:
    """pytest id prefixes of everything the plan removes: ``file::qualname`` and whole files."""
    ids = [f"{entry['file']}::{entry['test']}" for entry in plan["removed"]]
    return ids + [entry["file"] for entry in plan["deleted_files"]]


def is_removed(test_id: str, prefixes: list[str]) -> bool:
    """Whether ``test_id`` (``file::Class::test[param]``) is one of the removed tests."""
    for prefix in prefixes:
        if test_id == prefix or test_id.startswith(prefix + "[") or test_id.startswith(
                prefix + "::"):
            return True
    return False


def commit_cleaning(repo: Path, plan: dict, instance_id: str) -> str:
    """Write the plan into ``repo`` and commit it; return the new commit."""
    for path, text in plan["files"].items():
        (repo / path).write_bytes(text.encode("utf-8"))
    for path in plan["deleted"]:
        (repo / path).unlink()
    # An agent can read this message (git log in its container), so it does not name
    # the removed tests: the list is in the task's cleaning.json, outside the repository.
    lines = [COMMIT_SUBJECT, "", f"SWE-EVO task {instance_id} (v2)."]
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-F", "-", stdin="\n".join(lines) + "\n")
    return _git(repo, "rev-parse", "HEAD")


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                           *args], cwd=repo, env={**os.environ, **CLEAN_GIT_ENV},
                          capture_output=True, text=True, input=stdin, encoding="utf-8")
    if proc.returncode != 0:
        raise CleanError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


# ── running pytest in the task image ──

def staged_commit(task: SweevoTask, commit: str, dest: Path) -> Path:
    """``commit`` of the task repository in ``dest``, a git repository at that commit."""
    remove_tree(dest)
    gitops.export_commit(task.repo_dir, commit, dest)
    gitops.attach_repository(dest, task.repo_dir, commit)
    return dest


def run_in_image(task: SweevoTask, code: Path, command: str, timeout: float, checks=None):
    checks = checks or task_checks(task)
    with checks.session(code, timeout, f"sweevo-clean-{task.instance_id}") as run:
        return run(GIT_SETUP + command)


def collect_command(paths: list[str]) -> str:
    return ("python -m pytest --collect-only -q -p no:cacheprovider --color=no "
            "-W default::DeprecationWarning -W default::pytest.PytestDeprecationWarning "
            + " ".join(paths))


def collected_ids(output: str) -> list[str]:
    return sorted({line.strip() for line in output.splitlines()
                   if "::" in line and not line.startswith(("ERROR", "FAILED", " "))})


def check_collection(task: SweevoTask, v2_commit: str, plan: dict, work: Path,
                     checks=None) -> dict:
    """Collect the patch's test files on the base and on v2: v2 = base less the removed tests."""
    paths = sorted({entry["file"] for entry in plan["removed"]} | set(plan["files"]))
    result = {"files": paths, "ok": True}
    if not paths:
        return {**result, "note": "no file changed"}
    ids = {}
    for name, commit in (("base", task.base_ref), ("v2", v2_commit)):
        code = staged_commit(task, commit, work / name)
        try:
            outcome = run_in_image(task, code, collect_command(paths), COLLECT_TIMEOUT_S, checks)
        finally:
            remove_tree(code)
        if outcome.timed_out or outcome.exit_code not in (0, 5):
            raise CleanError(f"pytest cannot collect the cleaned files on {name}:\n"
                             f"{outcome.output[-3000:]}")
        ids[name] = collected_ids(outcome.output)
    prefixes = removed_ids(plan)
    expected = sorted(test_id for test_id in ids["base"] if not is_removed(test_id, prefixes))
    gone = sorted(test_id for test_id in ids["base"] if is_removed(test_id, prefixes))
    result.update({"base": len(ids["base"]), "v2": len(ids["v2"]), "removed_ids": gone,
                   "ok": ids["v2"] == expected})
    if not result["ok"]:
        missing = sorted(set(expected) - set(ids["v2"]))
        extra = sorted(set(ids["v2"]) - set(expected))
        raise CleanError(f"v2 collects other tests than the base less the removed ones: "
                         f"missing {missing[:20]}, extra {extra[:20]}")
    return result


def known_failing(task: SweevoTask, work: Path, prefixes: list[str], checks=None) -> dict:
    """Run the whole test suite on the base; the tests that fail or error, less removed ones."""
    tests_root = REPOS[task.repo]["tests"]
    command = test_command(task.test_cmd, [tests_root], task.test_timeout)
    code = staged_commit(task, task.base_ref, work / "suite")
    started = time.monotonic()
    try:
        outcome = run_in_image(task, code, command, SUITE_TIMEOUT_S, checks)
    finally:
        remove_tree(code)
    if outcome.timed_out or outcome.output.startswith("[sandbox error]"):
        raise CleanError(f"the test suite gave no result: {outcome.output[-2000:]}")
    parsed = task.parse(outcome.output)
    # The log parser also reads captured log lines ("ERROR    dvc.cli:cli.py:95 ...")
    # as statuses; only pytest ids of the test suite are tests.
    statuses = {test_id: status for test_id, status in parsed.items()
                if is_test_id(test_id, tests_root)}
    if not statuses:
        raise CleanError(f"no test ran (exit {outcome.exit_code}):\n{outcome.output[-3000:]}")
    failing = sorted(test_id for test_id, status in statuses.items() if status in FAILING)
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status] = counts.get(status, 0) + 1
    return {"command": command, "commit": task.base_ref, "exit_code": outcome.exit_code,
            "duration_s": round(time.monotonic() - started, 1), "statuses": counts,
            "failing": [test_id for test_id in failing if not is_removed(test_id, prefixes)],
            "failing_but_removed": [t for t in failing if is_removed(t, prefixes)],
            "not_test_ids": sorted(set(parsed) - set(statuses)),
            "output": outcome.output}


def is_test_id(test_id: str, tests_root: str) -> bool:
    """A pytest id (``path.py`` or ``path.py::name``) of a file under ``tests_root``."""
    path = test_id.split("::")[0]
    root = tests_root.rstrip("/")
    return path.endswith(".py") and path.startswith(root + "/")


# ── the request ──

def failing_section(failing: list[str], limit: int = LIST_LIMIT) -> str:
    """The section appended to ``request_iface.md``; built from the test run only."""
    lines = [SECTION_TITLE, ""]
    if not failing:
        lines += ["No test fails in this environment before any change.", ""]
        return "\n".join(lines)
    lines += [
        "The tests below already fail (or cannot be collected) in this environment before",
        "any change is made to the repository. The reason has nothing to do with this task:",
        "the environment's packages, not the code you are asked to change. You do not need",
        "to fix them, and do not spend time investigating the environment because of them.",
        "",
    ]
    if len(failing) > limit:
        lines += [f"{len(failing)} tests fail; the first {limit} in alphabetical order are "
                  f"listed here. The full list, `{KNOWN_FAILING}`, is kept with the task, "
                  "outside this repository.", ""]
    else:
        lines += [f"{len(failing)} test{'s' if len(failing) != 1 else ''} fail:", ""]
    lines += ["```text", *failing[:limit], "```", ""]
    return "\n".join(lines)


def patch_leaks(section: str, test_patch: str, removed: list[str]) -> list[str]:
    """What of the test patch the section shows: whole lines, or names of changed tests.

    A line the patch adds, removes or keeps as context (20 characters or
    more once stripped) must not be in the section, nor the name of a test
    that the patch adds or that the cleaning removed.
    """
    section_lines = {line.strip() for line in section.splitlines() if line.strip()}
    leaks = []
    for line in test_patch.splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "@@")):
            continue
        text = line[1:].strip() if line[:1] in "+- " else line.strip()
        if len(text) >= 20 and text in section_lines:
            leaks.append(f"line of the patch: {text}")
    words = set(re.findall(r"\w+", section))
    names = defined_test_names(test_patch) | {name.split("::")[-1] for name in removed}
    leaks += [f"test name: {name}" for name in sorted(names & words)]
    return leaks


def v2_request(request_iface: str, section: str) -> str:
    return request_iface.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"


# ── one task ──

def clean_task(instance_id: str, tasks: Path, out: Path, *, use_docker: bool = True,
               force: bool = False, checks=None) -> dict:
    """Write ``<out>/<id>/``; return what was done (also in ``cleaning.json``)."""
    source = tasks / instance_id
    dest = out / instance_id
    if source.resolve() == dest.resolve():
        raise CleanError("the v2 task cannot replace the original")
    task = load_task(source)
    if task.task_commit is None:
        raise CleanError(f"{source / 'grading' / 'instance.json'} has no task_commit")
    if gitops.git(task.repo_dir, "rev-list", "--max-parents=0", "HEAD").stdout.split() != [
            task.task_commit] or gitops.head(task.repo_dir) != task.task_commit:
        raise CleanError(f"{task.repo_dir} is not the one-commit repository of task_commit")
    if dest.exists():
        if not force:
            raise CleanError(f"{dest} exists (--force replaces it)")
        remove_tree(dest)
    dest.mkdir(parents=True)
    for item in source.iterdir():
        if item.name.startswith(("_", ".")):
            continue
        if item.is_dir():
            shutil.copytree(item, dest / item.name, symlinks=True)
        else:
            shutil.copy2(item, dest / item.name)
    v2 = dataclasses.replace(load_task(dest), exclusions=task.exclusions)
    plan = clean_plan(v2.repo_dir, v2.task_commit, v2.test_patch)
    commit = commit_cleaning(v2.repo_dir, plan, instance_id)
    report = {
        "instance_id": instance_id, "source_task": str(source), "task_commit": v2.task_commit,
        "cleaning_commit": commit, "commit_subject": COMMIT_SUBJECT,
        "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rule": ("every existing test function or method (parametrized ones included) that "
                 "grading/test.patch changes or deletes is removed from its file, located in "
                 "the base (task_commit); a test file the patch deletes is removed; tests the "
                 "patch adds and other tests of the same files are kept"),
        "python_files": plan["python_files"],
        "removed": plan["removed"], "removed_classes": plan["removed_classes"],
        "deleted_files": plan["deleted_files"], "untouched": plan["untouched"],
    }
    work = dest / "_work"
    try:
        if use_docker:
            print("  collecting the cleaned files (base and v2) ...", flush=True)
            report["collection"] = check_collection(v2, commit, plan, work, checks)
            print("  running the whole test suite on the base ...", flush=True)
            suite = known_failing(v2, work, removed_ids(plan), checks)
        else:
            report["collection"] = {"ok": None, "note": "--no-docker: not checked"}
            suite = None
    finally:
        remove_tree(work)
    request_iface = (dest / "request_iface.md").read_text(encoding="utf-8")
    if suite is not None:
        failing = suite.pop("failing")
        (dest / "known_failing.log").write_text(f"$ {suite['command']}\n{suite.pop('output')}",
                                               encoding="utf-8", newline="\n")
        header = [f"# Tests of {instance_id} that fail or error on the base ({v2.task_commit})",
                  "# in the task image, with the task's test command on the whole suite.",
                  "# Made by clean_tasks.py; one pytest id per line. Removed tests are left out.",
                  ""]
        (dest / KNOWN_FAILING).write_text("\n".join(header + failing) + "\n", encoding="utf-8",
                                          newline="\n")
        (dest / "known_failing.json").write_text(json.dumps(
            {**suite, "failing": len(failing)}, indent=1) + "\n", encoding="utf-8",
            newline="\n")
        section = failing_section(failing)
        leaks = patch_leaks(section, v2.test_patch, removed_ids(plan))
        if leaks:
            raise CleanError("the known-failing section would show the test patch: "
                             + "; ".join(leaks[:5]))
        (dest / REQUEST_V2).write_text(v2_request(request_iface, section), encoding="utf-8",
                                       newline="\n")
        report["known_failing"] = {"count": len(failing), "listed": min(len(failing), LIST_LIMIT),
                                   "removed_and_left_out": suite["failing_but_removed"],
                                   "log_lines_not_tests": len(suite["not_test_ids"])}
    request = (dest / "request.md").read_text(encoding="utf-8")
    checks_v1 = list(load_graph(source / "single.json").final_checks)
    dump_graph(single_graph(request, v2.repo_dir, checks_v1), dest / "single.json")
    if (dest / REQUEST_V2).is_file():
        graph = single_graph((dest / REQUEST_V2).read_text(encoding="utf-8"), v2.repo_dir,
                             checks_v1)
        dump_graph(graph.model_copy(update={"request_id": "single_iface"}),
                   dest / "single_iface.json")
    else:
        (dest / "single_iface.json").unlink(missing_ok=True)  # made with Docker only
    (dest / CLEANING).write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n",
                                 encoding="utf-8", newline="\n")
    return report


# ── validation: v2 against v1 with the base and the gold patch ──

def validate_v2(instance_id: str, tasks: Path, out: Path, work: Path) -> dict:
    """Grade base and gold of v1 and v2 (no exclusions); compare every listed test."""
    from validate_gold import fake_run, statuses  # noqa: PLC0415

    results = {}
    for version, root in (("v1", tasks), ("v2", out)):
        task = dataclasses.replace(load_task(root / instance_id), exclusions=())
        gold = (root / instance_id / "grading" / "gold.patch").read_text(encoding="utf-8")
        for name, patches in (("base", []), ("gold", [gold])):
            run_dir = fake_run(work / instance_id / f"{version}-{name}", task, patches)
            print(f"  {version} {name}: grading ...", flush=True)
            entry = evaluate_run(run_dir, task)
            if not entry["ok"] or not entry["tests_in_log"]:
                raise CleanError(f"{version} {name}: no result: {entry['error']}")
            results[(version, name)] = {"entry": entry, "tests": statuses(run_dir)}
            print(f"  {version} {name}: F2P {entry['f2p']['passed']}/{entry['f2p']['total']}  "
                  f"P2P {entry['p2p']['passed']}/{entry['p2p']['total']}", flush=True)
    report = {"instance_id": instance_id,
              "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    differences = {}
    for name in ("base", "gold"):
        v1, v2 = results[("v1", name)]["tests"], results[("v2", name)]["tests"]
        differences[name] = {kind: {test_id: [v1[kind][test_id], v2[kind].get(test_id)]
                                    for test_id in v1[kind] if v1[kind][test_id] !=
                                    v2[kind].get(test_id)} for kind in KINDS}
        for version in ("v1", "v2"):
            entry = results[(version, name)]["entry"]
            report[f"{version}_{name}"] = {"f2p": entry["f2p"], "p2p": entry["p2p"],
                                           "resolved": entry["resolved"],
                                           "duration_s": entry["duration_s"]}
    v2_base, v2_gold = report["v2_base"], report["v2_gold"]
    report["checks"] = {
        "v2 base: every F2P test fails": v2_base["f2p"]["passed"] == 0,
        "v2 gold: every F2P test passes": v2_gold["f2p"]["passed"] == v2_gold["f2p"]["total"],
        "every test's status is the one of v1 (base and gold)": not any(
            differences[name][kind] for name in differences for kind in KINDS),
    }
    report["differences"] = differences
    report["ok"] = all(report["checks"].values())
    (out / instance_id / "validation.json").write_text(json.dumps(report, indent=1) + "\n",
                                                      encoding="utf-8", newline="\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="+", metavar="instance_id")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="replace an existing v2 task")
    parser.add_argument("--no-docker", action="store_true",
                        help="no collection check, no test run (no known_failing.txt, "
                             "no request_iface_v2.md)")
    parser.add_argument("--validate", action="store_true",
                        help="then grade base and gold of v1 and v2 and compare them")
    parser.add_argument("--work", type=Path, default=Path(r"C:\tg\sweevo-validation-v2"))
    args = parser.parse_args(argv)
    failed = []
    for instance_id in args.ids:
        print(instance_id, flush=True)
        try:
            report = clean_task(instance_id, args.tasks, args.out, use_docker=not args.no_docker,
                                force=args.force)
            print(f"  removed {len(report['removed'])} test(s), "
                  f"{len(report['deleted_files'])} file(s); commit {report['cleaning_commit']}")
            for entry in report["removed"]:
                print(f"    {entry['file']}::{entry['test']}  ({entry['why']})")
            for entry in report["deleted_files"]:
                print(f"    {entry['file']}  (whole file: {entry['why']})")
            if "known_failing" in report:
                print(f"  known failing: {report['known_failing']['count']}")
            if args.validate:
                validation = validate_v2(instance_id, args.tasks, args.out, args.work)
                for check, ok in validation["checks"].items():
                    print(f"  [{'ok' if ok else 'FAIL'}] {check}")
                if not validation["ok"]:
                    failed.append(instance_id)
        except Exception as exc:  # noqa: BLE001 - report it and go on with the next one
            print(f"  FAILED: {type(exc).__name__}: {exc}", flush=True)
            failed.append(instance_id)
        finally:
            shutil.rmtree(args.work / instance_id, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
