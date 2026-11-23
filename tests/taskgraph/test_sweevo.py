"""SWE-EVO grading: log parsers, test command, grading, staging the code, ``sweevo-eval``.

No Docker: the checks are a fake that returns a canned pytest log.
"""
from __future__ import annotations

import dataclasses
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph import sandbox as sandbox_module
from aqours_code.taskgraph import sweevo as sweevo_module
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.process import ProcessResult
from aqours_code.taskgraph.sweevo import (
    EXTRA_PYTEST_OPTIONS,
    GIT_SETUP,
    SweevoError,
    evaluate_run,
    grade,
    load_skips,
    load_task,
    parse_log_pytest,
    parse_log_pytest_pydantic,
    patch_files,
    per_test,
    stage_code,
)
from aqours_code.taskgraph.sweevo import test_command as build_command
from aqours_code.taskgraph.sweevo import test_directives as directives_of
from taskgraph_support import commit_files, git

# The base: a package and its tests. The test patch changes one test file and
# adds another; the gold patch adds the feature.
BASE_FILES = {
    "pkg/__init__.py": "def old():\n    return 1\n",
    "tests/test_old.py": "from pkg import old\n\n\ndef test_old():\n    assert old() == 1\n",
    "README.md": "pkg\n",
}
TEST_PATCH = """\
diff --git a/tests/test_old.py b/tests/test_old.py
--- a/tests/test_old.py
+++ b/tests/test_old.py
@@ -3,3 +3,7 @@ from pkg import old

 def test_old():
     assert old() == 1
+
+
+def test_old_again():
+    assert old() + 1 == 2
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,5 @@
+from pkg import new
+
+
+def test_new():
+    assert new() == 2
diff --git a/tests/data.json b/tests/data.json
new file mode 100644
--- /dev/null
+++ b/tests/data.json
@@ -0,0 +1 @@
+{}
"""
GOLD_PATCH = """\
diff --git a/pkg/__init__.py b/pkg/__init__.py
--- a/pkg/__init__.py
+++ b/pkg/__init__.py
@@ -1,2 +1,6 @@
 def old():
     return 1
+
+
+def new():
+    return 2
"""
F2P = ["tests/test_new.py::test_new"]
P2P = ["tests/test_old.py::test_old", "tests/test_old.py::test_old_again"]
GOLD_LOG = """\
============================= test session starts ==============================
tests/test_new.py .                                                      [ 33%]
tests/test_old.py ..                                                     [100%]
=========================== short test summary info ============================
PASSED tests/test_new.py::test_new
PASSED tests/test_old.py::test_old
PASSED tests/test_old.py::test_old_again
============================== 3 passed in 0.02s ===============================
"""
BASE_LOG = """\
=========================== short test summary info ============================
PASSED tests/test_old.py::test_old
PASSED tests/test_old.py::test_old_again
ERROR tests/test_new.py - ImportError: cannot import name 'new' from 'pkg'
"""


class FakeChecks:
    """Checks that answer every command with ``log``; they record what they saw."""

    kind = "docker"
    image = "img:sweevo"

    def __init__(self, log: str, *, exit_code: int | None = 0, timed_out: bool = False,
                 env=(("PYTHONPATH", "/workspace"),)):
        self.log, self.exit_code, self.timed_out, self.env = log, exit_code, timed_out, env
        self.commands: list[str] = []
        self.seen: dict[str, str] = {}

    @contextmanager
    def session(self, cwd: Path, timeout: float, label: str):
        def run(command):
            self.commands.append(command)
            self.seen = {path.relative_to(cwd).as_posix(): path.read_text(encoding="utf-8")
                         for path in sorted(Path(cwd).rglob("*"))
                         if path.is_file() and ".git" not in path.relative_to(cwd).parts}
            return ProcessResult(command=command, exit_code=self.exit_code, output=self.log,
                                 timed_out=self.timed_out, duration_s=1.5)

        yield run


def make_task(root: Path, *, skips: str | None = None, log_parser: str = "parse_log_pytest",
              f2p=F2P, p2p=P2P) -> Path:
    task = root / "task"
    repo = task / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    commit_files(repo, BASE_FILES, "base")
    (task / "grading").mkdir()
    (task / "grading" / "instance.json").write_text(json.dumps({
        "instance_id": "owner__pkg_1.0_1.1", "repo": "owner/pkg", "base_commit": "abc123",
        "test_cmds": "pytest -rA", "log_parser": log_parser,
        "FAIL_TO_PASS": list(f2p), "PASS_TO_PASS": json.dumps(list(p2p))}), encoding="utf-8")
    (task / "grading" / "test.patch").write_text(TEST_PATCH, encoding="utf-8", newline="\n")
    (task / "grading" / "gold.patch").write_text(GOLD_PATCH, encoding="utf-8", newline="\n")
    (task / "sandbox.json").write_text(json.dumps({
        "image": "img:sweevo", "env": {"PYTHONPATH": "/workspace"}}), encoding="utf-8")
    if skips is not None:
        (task / "sandbox_skips.txt").write_text(skips, encoding="utf-8")
    return task


def make_run(root: Path, task: Path, *, final_files: dict[str, str] | None = None) -> Path:
    """A finished run: ``final/`` is the base with ``final_files`` written over it."""
    run = root / "run"
    final = run / "final"
    final.mkdir(parents=True)
    for relative, content in {**BASE_FILES, **(final_files or {})}.items():
        (final / relative).parent.mkdir(parents=True, exist_ok=True)
        (final / relative).write_text(content, encoding="utf-8", newline="\n")
    (final / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({
        "run_id": "r1", "status": "success", "wall_time_s": 100.0,
        "totals": {"input_tokens": 1000, "output_tokens": 100}}), encoding="utf-8")
    return run


# ── log parsers ──

def test_parse_log_pytest_reads_the_short_summary_like_swe_bench():
    log = "\n".join([
        "tests/test_a.py ..F",
        "PASSED tests/test_a.py::test_one",
        "PASSED tests/test_a.py::test_param[a-1]",
        "FAILED tests/test_a.py::test_two - AssertionError: assert 1 == 2",
        "ERROR tests/test_b.py::test_setup - fixture 'x' not found",
        "SKIPPED [1] tests/test_c.py:3: needs network",
        "XFAIL tests/test_a.py::test_known - reason",
        "PASSED",  # no id: ignored
        "PASSED tests/test_a.py::test_id with space",  # cut at the space, as SWE-bench does
        "  PASSED tests/test_a.py::indented",  # not at the line start: ignored
    ])
    assert parse_log_pytest(log) == {
        "tests/test_a.py::test_one": "PASSED",
        "tests/test_a.py::test_param[a-1]": "PASSED",
        "tests/test_a.py::test_two": "FAILED",
        "tests/test_b.py::test_setup": "ERROR",
        "[1]": "SKIPPED",
        "tests/test_a.py::test_known": "XFAIL",
        "tests/test_a.py::test_id": "PASSED",
    }


def test_parse_log_pytest_keeps_the_last_status_of_a_test():
    log = "FAILED tests/t.py::test_x - first\nPASSED tests/t.py::test_x\n"
    assert parse_log_pytest(log) == {"tests/t.py::test_x": "PASSED"}


def test_pydantic_parser_strips_colours_and_reads_verbose_lines():
    log = "\n".join([
        "\x1b[32mPASSED\x1b[0m tests/test_main.py::test_a",
        "tests/test_main.py::test_b PASSED",
        "tests/test_main.py::test_c FAILED",
        "FAILED [ 10%] tests/test_main.py::test_d - assert False",
        "tests/test_main.py::test_e SKIPPED",
    ])
    assert parse_log_pytest_pydantic(log) == {
        "tests/test_main.py::test_a": "PASSED",
        "tests/test_main.py::test_b": "PASSED",
        "tests/test_main.py::test_c": "FAILED",
        "tests/test_main.py::test_d": "FAILED",
        "tests/test_main.py::test_e": "SKIPPED",
    }


# ── the test command ──

def test_patch_files_and_directives_follow_swe_evo():
    patch = TEST_PATCH + """\
diff --git a/tests/test_gone.py b/tests/test_gone.py
deleted file mode 100644
--- a/tests/test_gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
diff --git a/tests/conftest.toml b/tests/conftest.toml
--- a/tests/conftest.toml
+++ b/tests/conftest.toml
@@ -1 +1 @@
-a
+b
"""
    files = patch_files(patch)
    assert [(item["new"], item["created"], item["deleted"]) for item in files] == [
        ("tests/test_old.py", False, False), ("tests/test_new.py", True, False),
        ("tests/data.json", True, False), ("tests/test_gone.py", False, True),
        ("tests/conftest.toml", False, False)]
    # deleted files and non-test extensions (.json, .toml, ...) are not run
    assert directives_of(patch, "owner/pkg") == ["tests/test_old.py", "tests/test_new.py"]
    dask_patch = patch + "diff --git a/dask/tests/data.cfg b/dask/tests/data.cfg\n"
    assert directives_of(dask_patch, "dask/dask") == ["tests/test_old.py",
                                                        "tests/test_new.py"]
    assert "dask/tests/data.cfg" in directives_of(dask_patch, "owner/pkg")


def test_test_command_adds_swe_evo_options_once_and_the_files():
    command = build_command("pytest --continue-on-collection-errors -n0 -rA  --color=no",
                           ["a/test_x.py", "b/test_y.py"])
    assert command == (f"{EXTRA_PYTEST_OPTIONS} --continue-on-collection-errors -n0 -rA  "
                       "--color=no a/test_x.py b/test_y.py")
    assert command.count("pytest") == 2  # the command name, and pytest.PytestDeprecationWarning
    timed = build_command("pytest -rA", ["t.py"], per_test_timeout=300)
    assert timed == f"{EXTRA_PYTEST_OPTIONS} --timeout=300 -rA t.py"


# ── grading ──

def test_grade_counts_like_swe_bench_and_computes_the_fix_rate():
    f2p = ["t::a", "t::b", "t::c", "t::d"]
    p2p = ["t::p", "t::q", "t::x"]
    status = {"t::a": "PASSED", "t::b": "XFAIL", "t::c": "FAILED", "t::p": "PASSED",
              "t::q": "SKIPPED", "t::x": "PASSED"}
    graded = grade(status, f2p, p2p)
    assert graded["f2p"] == {"total": 4, "passed": 2, "failed": 1, "errors": 0, "missing": 1,
                             "skipped": 0, "excluded": 0, "rate": 0.5}
    assert graded["p2p"]["passed"] == 2 and graded["p2p"]["skipped"] == 1
    assert graded["p2p"]["rate"] == 1.0  # a skipped test counts neither way
    assert (graded["resolved"], graded["fix_rate"], graded["q"]) == (False, 0.5, 0.5)

    status["t::x"] = "ERROR"  # one P2P failure: no fix rate
    graded = grade(status, f2p, p2p)
    assert graded["p2p"]["errors"] == 1 and graded["fix_rate"] == 0.0
    assert graded["q"] == pytest.approx(0.5 * 0.5)


def test_excluded_tests_leave_numerator_and_denominator():
    status = {"t::a": "PASSED", "t::b": "FAILED", "t::p": "PASSED"}
    graded = grade(status, ["t::a", "t::b"], ["t::p", "t::gone"], excluded=["t::b", "t::gone"])
    assert graded["f2p"]["total"] == 1 and graded["f2p"]["excluded"] == 1
    assert graded["p2p"]["total"] == 1 and graded["p2p"]["excluded"] == 1
    assert graded["resolved"] and graded["fix_rate"] == 1.0 and graded["q"] == 1.0
    assert per_test(status, ["t::a", "t::b"], ["t::gone"], ["t::b"]) == {
        "FAIL_TO_PASS": {"t::a": "PASSED", "t::b": "EXCLUDED"},
        "PASS_TO_PASS": {"t::gone": "MISSING"}}


def test_no_counted_test_gives_rate_one():
    graded = grade({}, [], ["t::p"], excluded=["t::p"])
    assert graded["f2p"]["rate"] == graded["p2p"]["rate"] == 1.0 and graded["resolved"]


# ── the task directory ──

def test_load_task_reads_the_grading_data_and_the_skips(tmp_path):
    task_dir = make_task(tmp_path, skips="# why\ntests/test_old.py::test_old_again\n\n")
    task = load_task(task_dir)
    assert (task.instance_id, task.repo, task.log_parser) == (
        "owner__pkg_1.0_1.1", "owner/pkg", "parse_log_pytest")
    assert task.fail_to_pass == tuple(F2P) and task.pass_to_pass == tuple(P2P)
    assert task.exclusions == ("tests/test_old.py::test_old_again",)
    assert task.sandbox.image == "img:sweevo"
    assert task.sandbox.env_dict == {"PYTHONPATH": "/workspace"}
    assert task.command == (f"{EXTRA_PYTEST_OPTIONS} -rA tests/test_old.py tests/test_new.py")
    other = tmp_path / "other.txt"
    other.write_text("tests/test_new.py::test_new\n", encoding="utf-8")
    assert load_task(task_dir, other).exclusions == ("tests/test_new.py::test_new",)
    assert load_skips(tmp_path / "missing.txt") == []


def test_load_task_reads_the_per_test_timeout(tmp_path):
    task_dir = make_task(tmp_path)
    instance = task_dir / "grading" / "instance.json"
    data = json.loads(instance.read_text(encoding="utf-8"))
    assert load_task(task_dir).test_timeout is None
    instance.write_text(json.dumps({**data, "test_timeout": 300}), encoding="utf-8")
    task = load_task(task_dir)
    assert task.test_timeout == 300 and " --timeout=300 -rA " in task.command
    instance.write_text(json.dumps({**data, "test_timeout": "300"}), encoding="utf-8")
    with pytest.raises(SweevoError):
        load_task(task_dir)


@pytest.mark.parametrize("damage", ["no_instance", "no_repo", "parser", "ids", "sandbox"])
def test_a_broken_task_directory_is_an_error(tmp_path, damage):
    task_dir = make_task(tmp_path)
    instance = task_dir / "grading" / "instance.json"
    data = json.loads(instance.read_text(encoding="utf-8"))
    if damage == "no_instance":
        instance.unlink()
    elif damage == "no_repo":
        (task_dir / "repo").rename(task_dir / "elsewhere")
    elif damage == "parser":
        instance.write_text(json.dumps({**data, "log_parser": "parse_log_django"}),
                            encoding="utf-8")
    elif damage == "ids":
        instance.write_text(json.dumps({**data, "FAIL_TO_PASS": "not json"}), encoding="utf-8")
    else:
        (task_dir / "sandbox.json").write_text('{"env": {}}', encoding="utf-8")
    with pytest.raises((SweevoError, ValueError)):
        load_task(task_dir)


# ── staging the code ──

def test_stage_code_restores_test_files_and_applies_the_test_patch(tmp_path):
    task = load_task(make_task(tmp_path))
    final = tmp_path / "final"
    final.mkdir()
    for relative, content in BASE_FILES.items():
        (final / relative).parent.mkdir(parents=True, exist_ok=True)
        (final / relative).write_text(content, encoding="utf-8", newline="\n")
    # The agent edited a test file of the patch and created one the patch adds.
    (final / "tests" / "test_old.py").write_text("def test_old():\n    pass\n",
                                                 encoding="utf-8", newline="\n")
    (final / "tests" / "test_new.py").write_text("agent's own\n", encoding="utf-8")
    (final / "tests" / "test_mine.py").write_text("def test_mine():\n    pass\n",
                                                  encoding="utf-8")
    (final / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    dest = tmp_path / "scratch"
    stage_code(final, dest, task)
    old = (dest / "tests" / "test_old.py").read_text(encoding="utf-8")
    assert old.startswith("from pkg import old") and "def test_old_again" in old
    assert (dest / "tests" / "test_new.py").read_text(encoding="utf-8").startswith(
        "from pkg import new")
    assert (dest / "tests" / "test_mine.py").is_file()  # other test files are kept
    assert (dest / "tests" / "data.json").read_text(encoding="utf-8") == "{}\n"
    assert (final / "tests" / "test_new.py").read_text(encoding="utf-8") == "agent's own\n"
    # A repository at the task's base commit, with everything as uncommitted changes
    # (like SWE-bench's /testbed); the run's own .git file is not copied.
    assert git(dest, "rev-parse", "HEAD") == git(task.repo_dir, "rev-parse", "HEAD")
    assert git(dest, "remote") == ""
    assert {line.strip() for line in git(dest, "status", "--porcelain").splitlines()} == {
        "M tests/test_old.py", "?? tests/data.json", "?? tests/test_mine.py",
        "?? tests/test_new.py"}


def _with_task_commit(task_dir: Path) -> str:
    """Record the repository's commit as instance.json's task_commit (as prepare.py does)."""
    instance = task_dir / "grading" / "instance.json"
    commit = git(task_dir / "repo", "rev-parse", "HEAD")
    data = json.loads(instance.read_text(encoding="utf-8"))
    instance.write_text(json.dumps({**data, "task_commit": commit}), encoding="utf-8")
    return commit


def _clean_like_v2(task_dir: Path) -> str:
    """A second commit without test_old, as clean_tasks.py makes a v2 task: the patch
    changes test_old's file, and its context no longer matches HEAD."""
    commit_files(task_dir / "repo", {"tests/test_old.py": "from pkg import old\n"},
                 "remove tests the release rewrites")
    return git(task_dir / "repo", "rev-parse", "HEAD")


def test_load_task_reads_the_task_commit(tmp_path):
    task_dir = make_task(tmp_path)
    assert load_task(task_dir).task_commit is None
    assert load_task(task_dir).base_ref == "HEAD"  # tasks without it: as before
    commit = _with_task_commit(task_dir)
    task = load_task(task_dir)
    assert task.task_commit == commit and task.base_ref == commit
    instance = task_dir / "grading" / "instance.json"
    data = json.loads(instance.read_text(encoding="utf-8"))
    instance.write_text(json.dumps({**data, "task_commit": ""}), encoding="utf-8")
    with pytest.raises(SweevoError, match="task_commit"):
        load_task(task_dir)


def test_stage_code_restores_the_test_files_from_the_base_not_head(tmp_path):
    task_dir = make_task(tmp_path)
    base = _with_task_commit(task_dir)
    head = _clean_like_v2(task_dir)
    task = load_task(task_dir)
    assert head != base and task.base_ref == base
    final = tmp_path / "final"
    gitops.export_commit(task.repo_dir, "HEAD", final)  # the v2 HEAD, as a worker starts
    assert "def test_old" not in (final / "tests" / "test_old.py").read_text(encoding="utf-8")
    dest = tmp_path / "scratch"
    stage_code(final, dest, task)
    old = (dest / "tests" / "test_old.py").read_text(encoding="utf-8")
    # the base's test_old is back, and the patch applied over it
    assert "def test_old():" in old and "def test_old_again" in old
    # the graded copy is a repository at the base, like for a v1 task
    assert git(dest, "rev-parse", "HEAD") == base
    assert {line.strip() for line in git(dest, "status", "--porcelain").splitlines()} == {
        "M tests/test_old.py", "?? tests/data.json", "?? tests/test_new.py"}


def test_restoring_from_head_would_break_the_v2_patch(tmp_path):
    """Why the base matters: over HEAD of a v2 task the test patch does not apply."""
    task_dir = make_task(tmp_path)
    _with_task_commit(task_dir)
    _clean_like_v2(task_dir)
    task = dataclasses.replace(load_task(task_dir), task_commit=None)  # base_ref = HEAD
    final = tmp_path / "final"
    gitops.export_commit(task.repo_dir, "HEAD", final)
    with pytest.raises(gitops.GitError):
        stage_code(final, tmp_path / "scratch", task)


def test_stage_code_of_a_v1_task_is_unchanged_by_the_task_commit(tmp_path):
    """In a v1 task task_commit is HEAD: the staged code is the same either way."""
    task_dir = make_task(tmp_path)
    final = make_run(tmp_path, task_dir, final_files={
        "pkg/__init__.py": "def old():\n    return 1\n\n\ndef new():\n    return 2\n"}) / "final"
    stage_code(final, tmp_path / "without", load_task(task_dir))
    _with_task_commit(task_dir)
    stage_code(final, tmp_path / "with", load_task(task_dir))

    def files(root: Path) -> dict:
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file() and ".git" not in path.relative_to(root).parts}

    assert files(tmp_path / "without") == files(tmp_path / "with")
    assert git(tmp_path / "without", "rev-parse", "HEAD") == git(tmp_path / "with",
                                                                   "rev-parse", "HEAD")


def test_a_task_commit_missing_from_the_repository_is_an_error(tmp_path):
    task_dir = make_task(tmp_path)
    task = dataclasses.replace(load_task(task_dir), task_commit="0" * 40)
    with pytest.raises(SweevoError, match="is not in"):
        stage_code(None, tmp_path / "scratch", task, commit=(task.repo_dir, "HEAD"))


def test_stage_code_from_a_commit_with_the_gold_patch(tmp_path):
    task = load_task(make_task(tmp_path))
    dest = tmp_path / "scratch"
    stage_code(None, dest, task, commit=(task.repo_dir, "HEAD"), patches=[GOLD_PATCH])
    assert "def new():" in (dest / "pkg" / "__init__.py").read_text(encoding="utf-8")
    assert (dest / "tests" / "test_new.py").is_file()
    assert "M pkg/__init__.py" in {line.strip()
                                   for line in git(dest, "status", "--porcelain").splitlines()}
    assert git(task.repo_dir, "status", "--porcelain") == ""  # the task repo is untouched


# ── evaluating a run ──

def test_evaluate_run_grades_final_and_writes_the_results(tmp_path):
    task = load_task(make_task(tmp_path))
    run = make_run(tmp_path, task.task_dir, final_files={
        "pkg/__init__.py": BASE_FILES["pkg/__init__.py"] + "\n\ndef new():\n    return 2\n"})
    checks = FakeChecks(GOLD_LOG)
    entry = evaluate_run(run, task, checks)
    assert checks.commands == [GIT_SETUP + task.command]
    assert GIT_SETUP.startswith('mkdir -p "${HOME:-/tmp/home}" && git config --global')
    assert "def test_old_again" in checks.seen["tests/test_old.py"]  # the patch was applied
    assert entry["ok"] and entry["resolved"] and entry["fix_rate"] == 1.0 and entry["q"] == 1.0
    assert (entry["f2p"]["passed"], entry["f2p"]["total"]) == (1, 1)
    assert (entry["p2p"]["passed"], entry["p2p"]["total"]) == (2, 2)
    assert entry["code"] == "final" and entry["tests_in_log"] == 3
    assert entry["sandbox_image"] == "img:sweevo"
    assert entry["sandbox_env"] == {"PYTHONPATH": "/workspace"}
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["sweevo"] == entry and summary["status"] == "success"
    results = json.loads((run / "sweevo_results.json").read_text(encoding="utf-8"))
    assert results["tests"]["FAIL_TO_PASS"] == {"tests/test_new.py::test_new": "PASSED"}
    assert (run / "sweevo_tests.txt").read_text(encoding="utf-8").startswith(
        f"$ {task.command}\n")
    assert not (run / "sweevo_eval").exists()


def test_evaluate_run_without_the_feature(tmp_path):
    task = load_task(make_task(tmp_path, skips="tests/test_old.py::test_old_again\n"))
    run = make_run(tmp_path, task.task_dir)
    entry = evaluate_run(run, task, FakeChecks(BASE_LOG, exit_code=2))
    assert entry["ok"]  # the tests ran; they failed
    assert entry["f2p"]["missing"] == 1 and entry["f2p"]["rate"] == 0.0
    assert entry["p2p"] == {"total": 1, "passed": 1, "failed": 0, "errors": 0, "missing": 0,
                            "skipped": 0, "excluded": 1, "rate": 1.0}
    assert (entry["resolved"], entry["fix_rate"], entry["q"]) == (False, 0.0, 0.0)
    assert entry["excluded_tests"] == 1


def test_evaluate_run_uses_the_integration_commit_without_final(tmp_path):
    task = load_task(make_task(tmp_path))
    run = tmp_path / "run"
    run.mkdir()
    repo = run / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    commit = commit_files(repo, {**BASE_FILES, "pkg/__init__.py": "def old():\n    return 1\n"
                                 "\n\ndef new():\n    return 2\n"}, "merged")
    (run / "summary.json").write_text(json.dumps({"integration_commit": commit}),
                                      encoding="utf-8")
    checks = FakeChecks(GOLD_LOG)
    entry = evaluate_run(run, task, checks, keep_scratch=True)
    assert entry["code"] == "integration_commit"
    assert "def new():" in checks.seen["pkg/__init__.py"]
    assert (run / "sweevo_eval" / "tests" / "test_new.py").is_file()  # kept on request
    assert (run / "sweevo_eval" / ".git").is_dir()


@pytest.mark.parametrize("problem", ["timeout", "sandbox"])
def test_a_test_run_without_a_result_is_not_ok(tmp_path, problem):
    task = load_task(make_task(tmp_path))
    run = make_run(tmp_path, task.task_dir)
    if problem == "timeout":
        checks = FakeChecks("partial output\n", exit_code=None, timed_out=True)
    else:
        checks = FakeChecks("[sandbox error] SandboxError: no such image\n", exit_code=None)
    entry = evaluate_run(run, task, checks, timeout=60)
    assert not entry["ok"]
    assert ("timed out after 60 s" if problem == "timeout" else "no such image") in entry["error"]


def test_a_run_without_code_or_summary_is_an_error(tmp_path):
    task = load_task(make_task(tmp_path))
    with pytest.raises(SweevoError, match="summary.json"):
        evaluate_run(tmp_path / "nothing", task, FakeChecks(GOLD_LOG))
    run = tmp_path / "bare"
    run.mkdir()
    (run / "summary.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SweevoError, match="neither final/"):
        evaluate_run(run, task, FakeChecks(GOLD_LOG))


def test_task_checks_run_in_the_task_image_with_its_environment(tmp_path):
    task = load_task(make_task(tmp_path))
    checks = sweevo_module.task_checks(task)
    assert (checks.kind, checks.image, checks.env) == (
        "docker", "img:sweevo", (("PYTHONPATH", "/workspace"),))
    with pytest.raises(SweevoError):
        sweevo_module.task_checks(dataclasses.replace(
            task, sandbox=sandbox_module.SandboxConfig(kind="none")))


# ── the command ──

def test_sweevo_eval_command_prints_and_records(tmp_path, monkeypatch, capsys):
    task_dir = make_task(tmp_path)
    run = make_run(tmp_path, task_dir)
    checked = []
    monkeypatch.setattr(sandbox_module, "check_docker", lambda image: checked.append(image))
    monkeypatch.setattr(sweevo_module, "task_checks", lambda task: FakeChecks(BASE_LOG))
    code = main(["sweevo-eval", str(run), "--task", str(task_dir)])
    out = capsys.readouterr().out
    assert code == 0 and checked == ["img:sweevo"]
    assert "owner__pkg_1.0_1.1: F2P 0/1  P2P 2/2  resolved: False  fix rate: 0.000" in out
    assert json.loads((run / "summary.json").read_text(encoding="utf-8"))["sweevo"]["ok"]

    monkeypatch.setattr(sweevo_module, "task_checks",
                        lambda task: FakeChecks("", exit_code=None, timed_out=True))
    assert main(["sweevo-eval", str(run), "--task", str(task_dir)]) == 1
    assert "gave no result" in capsys.readouterr().err


def test_sweevo_eval_command_input_errors(tmp_path, monkeypatch, capsys):
    task_dir = make_task(tmp_path)
    run = make_run(tmp_path, task_dir)
    assert main(["sweevo-eval", str(tmp_path / "none"), "--task", str(task_dir)]) == 2
    assert main(["sweevo-eval", str(run), "--task", str(tmp_path / "none")]) == 2

    def unavailable(image):
        raise sandbox_module.SandboxUnavailable(f"Docker image {image} not found.")

    monkeypatch.setattr(sandbox_module, "check_docker", unavailable)
    assert main(["sweevo-eval", str(run), "--task", str(task_dir)]) == 2
    err = capsys.readouterr().err
    assert "not a prepared SWE-EVO task" in err and "img:sweevo not found" in err
    assert "sweevo" not in json.loads((run / "summary.json").read_text(encoding="utf-8"))
