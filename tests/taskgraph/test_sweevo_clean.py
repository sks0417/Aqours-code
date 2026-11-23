"""The cleaned SWE-EVO tasks (experiments/taskgraph/sweevo/clean_tasks.py).

No model and no Docker: the test patch here comes from ``git diff`` of a
small repository, pytest collects on the host, and the whole-suite run in the
task image is a fake that returns a pytest log.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph.process import ProcessResult
from aqours_code.taskgraph.schema import load_graph
from aqours_code.taskgraph.single import single_graph
from aqours_code.taskgraph.schema import dump_graph
from taskgraph_support import commit_files, git

SWEEVO = Path(__file__).resolve().parents[2] / "experiments" / "taskgraph" / "sweevo"


def _load():
    sys.path.insert(0, str(SWEEVO))
    spec = importlib.util.spec_from_file_location("clean_tasks", SWEEVO / "clean_tasks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


ct = _load()

BASE_TESTS = '''\
import pytest


def helper():
    return 1


def test_kept():
    assert helper() == 1


@pytest.mark.parametrize("x", [1, 2])
def test_param(x):
    assert x


def test_changed():
    assert helper() == 1


class TestGroup:
    @pytest.fixture
    def value(self):
        return 1

    def test_method_changed(self, value):
        assert value == 1

    def test_method_kept(self, value):
        assert value


class TestOnlyChanged:
    """All its tests change."""

    def test_a(self):
        assert True


def test_deleted():
    assert "the old behaviour"


def test_last():
    assert True
'''
# the release: test_param gets a third value, test_changed and two methods change,
# test_deleted goes, a new test comes, helper and the fixture change (not tests)
RELEASE_TESTS = '''\
import pytest


def helper():
    return 2


def test_kept():
    assert helper() == 1


@pytest.mark.parametrize("x", [1, 2, 3])
def test_param(x):
    assert x


def test_changed():
    assert helper() == 2


class TestGroup:
    @pytest.fixture
    def value(self):
        return 2

    def test_method_changed(self, value):
        assert value == 2

    def test_method_kept(self, value):
        assert value


class TestOnlyChanged:
    """All its tests change."""

    def test_a(self):
        assert not False


def test_last():
    assert True
    assert True


def test_brand_new():
    assert True
'''
BASE_FILES = {
    "pkg/__init__.py": "VALUE = 1\n",
    "tests/test_mod.py": BASE_TESTS,
    "tests/test_gone.py": "def test_gone():\n    assert True\n",
    "tests/test_other.py": "def test_other():\n    assert True\n",
    "tests/helpers.py": "def make():\n    return 1\n",
}
RELEASE_FILES = {
    "tests/test_mod.py": RELEASE_TESTS,
    "tests/test_new.py": "def test_new():\n    assert True\n",
    "tests/helpers.py": "def make():\n    return 2\n",
}
REMOVED = ["test_param", "test_changed", "TestGroup::test_method_changed",
           "TestOnlyChanged::test_a", "test_deleted", "test_last"]


def make_repo(root: Path) -> tuple[Path, str, str]:
    """The base repository and the release's test patch (``git diff`` of the release)."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    base = commit_files(repo, BASE_FILES, "base")
    work = root / "release"
    git(root, "clone", "-q", str(repo), work.name)
    for path, text in RELEASE_FILES.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text, encoding="utf-8", newline="\n")
    (work / "tests" / "test_gone.py").unlink()
    git(work, "add", "-A")
    patch = git(work, "diff", "--cached", "HEAD") + "\n"
    return repo, base, patch


# ── the patch ──


def test_parse_patch_reads_files_and_old_line_numbers(tmp_path):
    _, _, patch = make_repo(tmp_path)
    files = {diff.old: diff for diff in ct.parse_patch(patch)}
    assert files["tests/test_gone.py"].deleted and not files["tests/test_gone.py"].created
    assert files["tests/test_new.py"].created
    mod = files["tests/test_mod.py"]
    assert not (mod.created or mod.deleted or mod.renamed)
    removed = [line for hunk in mod.hunks for line in hunk.removed]
    base_lines = BASE_TESTS.splitlines()
    assert {base_lines[line - 1].strip() for line in removed} >= {
        "return 1", '@pytest.mark.parametrize("x", [1, 2])', "def test_deleted():",
        "assert helper() == 1", "assert value == 1"}
    assert [hunk.index for hunk in mod.hunks] == list(range(1, len(mod.hunks) + 1))
    inserted = [lines for hunk in mod.hunks for _, lines in hunk.inserted]
    assert any("def test_brand_new():" in line for lines in inserted for line in lines)


def test_parse_patch_handles_a_hunk_that_only_inserts():
    patch = ("diff --git a/t/test_x.py b/t/test_x.py\n--- a/t/test_x.py\n+++ b/t/test_x.py\n"
             "@@ -2,0 +3,2 @@ def test_a():\n+    assert 1\n+    assert 2\n")
    (diff,) = ct.parse_patch(patch)
    (hunk,) = diff.hunks
    assert hunk.removed == [] and hunk.inserted == [(2, ["    assert 1", "    assert 2"])]


def test_defined_test_names_are_the_tests_the_patch_adds(tmp_path):
    _, _, patch = make_repo(tmp_path)
    assert {"test_brand_new", "test_new"} <= ct.defined_test_names(patch)


# ── which tests a hunk changes ──


def _hunks(base: str, new: str) -> list:
    patch = ct_diff(base, new)
    return ct.parse_patch(patch)[0].hunks


def ct_diff(base: str, new: str) -> str:
    import difflib
    lines = difflib.unified_diff(base.splitlines(True), new.splitlines(True),
                                 "a/tests/test_x.py", "b/tests/test_x.py", n=1)
    return "diff --git a/tests/test_x.py b/tests/test_x.py\n" + "".join(lines)


def _touched(base: str, new: str) -> list[str]:
    definitions = ct.test_definitions(base)
    names = set()
    for hunk in _hunks(base, new):
        names.update(item.qualname for item, _ in ct.touched_tests(definitions, hunk))
    return sorted(names)


TWO = "def test_a():\n    x = 1\n    assert x\n\n\ndef test_b():\n    assert True\n"


def test_a_line_added_after_the_last_line_of_a_test_changes_it():
    new = TWO.replace("    assert x\n", "    assert x\n    assert x == 1\n")
    assert _touched(TWO, new) == ["test_a"]


def test_a_new_test_between_tests_changes_none():
    new = TWO.replace("\n\ndef test_b", "\n\ndef test_c():\n    assert 1\n\n\ndef test_b")
    assert _touched(TWO, new) == []


def test_a_new_decorator_right_above_a_test_changes_it():
    new = TWO.replace("def test_b", "@pytest.mark.slow\ndef test_b")
    assert _touched(TWO, new) == ["test_b"]


def test_a_renamed_test_class_changes_all_its_tests():
    base = "class TestA:\n    def test_x(self):\n        pass\n\n    def test_y(self):\n        pass\n"
    assert _touched(base, base.replace("class TestA:", "class TestRenamed:")) == [
        "TestA::test_x", "TestA::test_y"]


def test_changing_a_non_test_member_of_a_class_changes_no_test():
    base = ("class TestA:\n    value = 1\n\n    def helper(self):\n        return 1\n\n"
            "    def test_x(self):\n        pass\n")
    new = base.replace("value = 1", "value = 2").replace("return 1", "return 2")
    assert _touched(base, new) == []


# ── the plan and the files it writes ──


def test_clean_plan_removes_exactly_the_changed_and_deleted_tests(tmp_path):
    repo, base, patch = make_repo(tmp_path)
    plan = ct.clean_plan(repo, base, patch)
    assert sorted(entry["test"] for entry in plan["removed"]) == sorted(REMOVED)
    by_name = {entry["test"]: entry for entry in plan["removed"]}
    assert by_name["test_param"]["parametrized"] is True
    assert by_name["test_deleted"]["why"] == "deleted"
    assert by_name["test_changed"]["why"] == "changed"
    assert by_name["TestGroup::test_method_changed"]["kind"] == "method"
    for entry in plan["removed"]:
        assert entry["file"] == "tests/test_mod.py"
        assert entry["hunks"] and all(hunk["header"].startswith("@@ ") for hunk in entry["hunks"])
        start, end = entry["lines"]
        assert BASE_TESTS.splitlines()[start - 1].lstrip().startswith(("def ", "@"))
        assert end >= start
    assert plan["removed_classes"] == [{"file": "tests/test_mod.py", "class": "TestOnlyChanged",
                                        "why": "all its tests are removed"}]
    assert plan["deleted"] == ["tests/test_gone.py"]
    assert plan["deleted_files"][0]["tests"] == ["test_gone"]
    untouched = {entry["file"]: entry["why"] for entry in plan["untouched"]}
    assert untouched["tests/test_new.py"] == "the patch creates it"
    assert untouched["tests/helpers.py"].startswith("not a test module")
    text = plan["files"]["tests/test_mod.py"]
    ast.parse(text)
    for name in ("def helper", "def test_kept", "def value", "def test_method_kept",
                 "class TestGroup"):
        assert name in text
    for name in ("test_param", "test_changed", "test_method_changed", "TestOnlyChanged",
                 "test_deleted", "test_last"):
        assert name not in text
    assert "\n\n\n\n" not in text and text.endswith("\n")


def test_the_cleaned_files_still_collect_with_pytest(tmp_path):
    repo, base, patch = make_repo(tmp_path)
    plan = ct.clean_plan(repo, base, patch)

    def collect(root: Path) -> set[str]:
        proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                               "-p", "no:cacheprovider", "tests"], cwd=root,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return set(ct.collected_ids(proc.stdout))

    before = collect(repo)
    ct.commit_cleaning(repo, plan, "owner__pkg_1.0_1.1")
    after = collect(repo)
    prefixes = ct.removed_ids(plan)
    assert after == {test_id for test_id in before if not ct.is_removed(test_id, prefixes)}
    assert "tests/test_mod.py::test_param[1]" in before and not any(
        "test_param" in test_id for test_id in after)
    assert "tests/test_other.py::test_other" in after


def test_remove_definitions_keeps_the_spacing_of_the_rest():
    source = ("import x\n\n\ndef test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\n"
              "def test_c():\n    pass\n")
    definitions = ct.test_definitions(source)
    by_name = {item.qualname: item for item in definitions}
    middle, _ = ct.remove_definitions(source, [by_name["test_b"]], definitions)
    assert middle == "import x\n\n\ndef test_a():\n    pass\n\n\ndef test_c():\n    pass\n"
    first, _ = ct.remove_definitions(source, [by_name["test_a"]], definitions)
    assert first == "import x\n\n\ndef test_b():\n    pass\n\n\ndef test_c():\n    pass\n"
    last, _ = ct.remove_definitions(source, [by_name["test_c"]], definitions)
    assert last == "import x\n\n\ndef test_a():\n    pass\n\n\ndef test_b():\n    pass\n"


def test_commit_cleaning_is_a_second_commit_with_a_fixed_author_and_date(tmp_path):
    repo, base, patch = make_repo(tmp_path)
    commit = ct.commit_cleaning(repo, ct.clean_plan(repo, base, patch), "owner__pkg_1.0_1.1")
    assert git(repo, "rev-parse", "HEAD~1") == base
    log = git(repo, "log", "-1", "--format=%an|%ae|%aI|%cI|%s")
    assert log == ("SWE-EVO Task|task@example.invalid|2026-01-02T00:00:00Z|"
                   "2026-01-02T00:00:00Z|remove tests the release rewrites")
    # the message names no removed test: an agent can read it with git log
    body = git(repo, "log", "-1", "--format=%b")
    assert body == "SWE-EVO task owner__pkg_1.0_1.1 (v2)."
    assert not any(name.split("::")[-1] in body for name in REMOVED + ["test_gone"])
    assert not (repo / "tests" / "test_gone.py").exists()
    assert git(repo, "status", "--porcelain") == ""
    # the same input gives the same commit
    repo2, base2, patch2 = make_repo(tmp_path / "again")
    assert ct.commit_cleaning(repo2, ct.clean_plan(repo2, base2, patch2),
                              "owner__pkg_1.0_1.1") == commit


def test_only_pytest_ids_of_the_test_suite_are_tests():
    assert ct.is_test_id("tests/a.py::test_x[1]", "tests")
    assert ct.is_test_id("tests/unit/command/test_dag.py", "tests")  # did not collect
    assert ct.is_test_id("dask/tests/test_x.py::test_y", "dask")
    for junk in ("dvc.cli:cli.py:95", "dvc:main.py:82", "tests", "other/test_a.py::t",
                 "testsuite/test_a.py::t"):
        assert not ct.is_test_id(junk, "tests"), junk


def test_is_removed_matches_parameters_and_methods_only():
    prefixes = ["tests/a.py::test_x", "tests/a.py::TestA::test_y", "tests/gone.py"]
    assert ct.is_removed("tests/a.py::test_x", prefixes)
    assert ct.is_removed("tests/a.py::test_x[1-2]", prefixes)
    assert ct.is_removed("tests/a.py::TestA::test_y[p]", prefixes)
    assert ct.is_removed("tests/gone.py::test_z", prefixes)
    assert not ct.is_removed("tests/a.py::test_x_other", prefixes)
    assert not ct.is_removed("tests/a.py::TestA::test_yy", prefixes)


# ── the request ──


def test_the_section_lists_every_test_up_to_sixty():
    few = [f"tests/a.py::test_{n}" for n in range(3)]
    section = ct.failing_section(few)
    assert section.startswith(ct.SECTION_TITLE + "\n")
    assert "3 tests fail:" in section and all(test_id in section for test_id in few)
    assert "do not spend time investigating the environment" in section
    many = sorted(f"tests/a.py::test_{n:03d}" for n in range(75))
    section = ct.failing_section(many)
    assert "75 tests fail; the first 60" in section and ct.KNOWN_FAILING in section
    assert "outside this repository" in section  # the agent cannot open the full list
    assert many[59] in section and many[60] not in section
    assert "No test fails" in ct.failing_section([])


def test_patch_leaks_finds_patch_lines_and_changed_test_names(tmp_path):
    repo, base, patch = make_repo(tmp_path)
    removed = ct.removed_ids(ct.clean_plan(repo, base, patch))
    clean = ct.failing_section(["tests/test_other.py::test_other"])
    assert ct.patch_leaks(clean, patch, removed) == []
    assert ct.patch_leaks(ct.failing_section(["tests/test_mod.py::test_brand_new"]), patch,
                          removed) == ["test name: test_brand_new"]
    assert ct.patch_leaks(ct.failing_section(["tests/test_mod.py::test_changed"]), patch,
                          removed) == ["test name: test_changed"]
    leaky = clean + '\n@pytest.mark.parametrize("x", [1, 2, 3])\n'
    assert any(leak.startswith("line of the patch") for leak in ct.patch_leaks(
        leaky, patch, removed))


# ── a whole task, with a fake task image ──


class FakeImage:
    """Collection runs pytest on the host; the whole suite answers with ``log``."""

    kind = "docker"

    def __init__(self, log: str):
        self.log = log
        self.commands: list[str] = []

    @contextmanager
    def session(self, cwd: Path, timeout: float, label: str):
        def run(command: str):
            self.commands.append(command)
            assert command.startswith(ct.GIT_SETUP)
            command = command[len(ct.GIT_SETUP):]
            if "--collect-only" in command:
                args = command.split()[2:]  # after "python -m"
                proc = subprocess.run([sys.executable, "-m", *args], cwd=cwd,
                                      capture_output=True, text=True, timeout=120)
                return ProcessResult(command=command, exit_code=proc.returncode,
                                     output=proc.stdout + proc.stderr, timed_out=False,
                                     duration_s=0.1)
            assert git(cwd, "rev-parse", "HEAD") == self.base  # the suite runs on the base
            return ProcessResult(command=command, exit_code=1, output=self.log,
                                 timed_out=False, duration_s=2.0)

        yield run


SUITE_LOG = """\
=========================== short test summary info ============================
PASSED tests/test_mod.py::test_kept
FAILED tests/test_other.py::test_other - ImportError: newer package
ERROR tests/test_broken.py - ModuleNotFoundError: No module named 'gone'
FAILED tests/test_mod.py::test_last - AssertionError
ERROR    pkg.cli:cli.py:95 unexpected error
"""


def make_task(root: Path) -> Path:
    """An original task directory as prepare.py writes it (no image needed here)."""
    tasks = root / "tasks"
    task = tasks / "owner__pkg_1.0_1.1"
    repo, base, patch = make_repo(task)
    (task / "release").rename(root / "release")
    (task / "grading").mkdir()
    (task / "grading" / "test.patch").write_text(patch, encoding="utf-8", newline="\n")
    (task / "grading" / "gold.patch").write_text("", encoding="utf-8")
    (task / "grading" / "instance.json").write_text(json.dumps({
        "instance_id": "owner__pkg_1.0_1.1", "repo": "iterative/dvc", "base_commit": "abc",
        "task_commit": base, "test_cmds": "pytest --continue-on-collection-errors -rA",
        "log_parser": "parse_log_pytest", "FAIL_TO_PASS": ["tests/test_mod.py::test_brand_new"],
        "PASS_TO_PASS": ["tests/test_mod.py::test_kept"]}), encoding="utf-8")
    (task / "sandbox.json").write_text(json.dumps({"image": "img:sweevo"}), encoding="utf-8")
    (task / "sandbox_skips.txt").write_text("# none\n", encoding="utf-8")
    (task / "request.md").write_text("Make the release.\n", encoding="utf-8", newline="\n")
    (task / "request_iface.md").write_text("Make the release.\n\n## Interface of this "
                                           "release\n\nNothing.\n", encoding="utf-8",
                                           newline="\n")
    checks = ["python -c \"import pkg\"", "python -m pytest --collect-only -qq tests"]
    dump_graph(single_graph("Make the release.\n", repo, checks), task / "single.json")
    return tasks


def test_clean_task_writes_the_v2_task_and_leaves_the_original_alone(tmp_path):
    tasks = make_task(tmp_path)
    source = tasks / "owner__pkg_1.0_1.1"
    before = {path.relative_to(source).as_posix(): path.read_bytes()
              for path in source.rglob("*") if path.is_file()}
    image = FakeImage(SUITE_LOG)
    image.base = git(source / "repo", "rev-parse", "HEAD")
    out = tmp_path / "v2"
    report = ct.clean_task("owner__pkg_1.0_1.1", tasks, out, checks=image)
    after = {path.relative_to(source).as_posix(): path.read_bytes()
             for path in source.rglob("*") if path.is_file()}
    assert after == before  # the original task is untouched
    dest = out / "owner__pkg_1.0_1.1"
    for name in ("repo", "grading", "request.md", "request_iface.md", "sandbox.json",
                 "sandbox_skips.txt", "single.json", "single_iface.json", "cleaning.json",
                 "known_failing.txt", "known_failing.json", "known_failing.log",
                 ct.REQUEST_V2):
        assert (dest / name).exists(), name
    assert not (dest / "_work").exists()
    assert (dest / "grading" / "test.patch").read_bytes() == (
        source / "grading" / "test.patch").read_bytes()
    # the repository: the original commit, then the cleaning
    repo = dest / "repo"
    assert git(repo, "rev-list", "--max-parents=0", "HEAD") == image.base
    assert git(repo, "rev-parse", "HEAD~1") == image.base
    assert git(repo, "log", "-1", "--format=%s") == ct.COMMIT_SUBJECT
    # the manifest
    saved = json.loads((dest / "cleaning.json").read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(report))
    assert saved["task_commit"] == image.base and saved["cleaning_commit"] == git(
        repo, "rev-parse", "HEAD")
    assert sorted(entry["test"] for entry in saved["removed"]) == sorted(REMOVED)
    assert saved["collection"]["ok"] is True and saved["collection"]["v2"] < saved[
        "collection"]["base"]
    # the tests that already fail: the removed test_last is left out
    listed = [line for line in (dest / "known_failing.txt").read_text(
        encoding="utf-8").splitlines() if line and not line.startswith("#")]
    assert listed == ["tests/test_broken.py", "tests/test_other.py::test_other"]
    assert saved["known_failing"] == {"count": 2, "listed": 2, "log_lines_not_tests": 1,
                                      "removed_and_left_out": ["tests/test_mod.py::test_last"]}
    suite = json.loads((dest / "known_failing.json").read_text(encoding="utf-8"))
    assert suite["command"].endswith(" -rA tests")  # the task's command, the whole suite
    assert suite["commit"] == image.base and suite["failing"] == 2
    assert suite["statuses"] == {"PASSED": 1, "FAILED": 2, "ERROR": 1}
    assert suite["not_test_ids"] == ["pkg.cli:cli.py:95"]  # a captured log line
    # the request: request_iface.md unchanged, then the section, nothing of the patch
    request_iface = (dest / "request_iface.md").read_text(encoding="utf-8")
    request_v2 = (dest / ct.REQUEST_V2).read_text(encoding="utf-8")
    assert request_v2.startswith(request_iface.rstrip("\n") + "\n\n" + ct.SECTION_TITLE)
    section = request_v2[len(request_iface.rstrip("\n")):]
    assert "tests/test_other.py::test_other" in section and "test_last" not in section
    patch = (dest / "grading" / "test.patch").read_text(encoding="utf-8")
    assert ct.patch_leaks(section, patch, ct.removed_ids(saved)) == []
    for line in patch.splitlines():
        if line[:1] in "+-" and len(line[1:].strip()) >= 8:
            assert line[1:].strip() not in section
    # the graphs: at the v2 HEAD, single_iface with the v2 request
    head = git(repo, "rev-parse", "HEAD")
    single, iface = load_graph(dest / "single.json"), load_graph(dest / "single_iface.json")
    assert single.base_commit == iface.base_commit == head
    assert single.request == "Make the release.\n"
    assert iface.request == request_v2 and iface.request_id == "single_iface"
    assert iface.final_checks == load_graph(source / "single.json").final_checks
    assert request_v2 in iface.nodes[0].goal
    # an existing v2 task is not replaced without --force
    with pytest.raises(ct.CleanError, match="--force"):
        ct.clean_task("owner__pkg_1.0_1.1", tasks, out, checks=image)
    ct.clean_task("owner__pkg_1.0_1.1", tasks, out, checks=image, force=True)


def test_clean_task_without_docker_makes_no_v2_request(tmp_path):
    tasks = make_task(tmp_path)
    out = tmp_path / "v2"
    report = ct.clean_task("owner__pkg_1.0_1.1", tasks, out, use_docker=False)
    dest = out / "owner__pkg_1.0_1.1"
    assert report["collection"]["ok"] is None
    assert not (dest / ct.REQUEST_V2).exists() and not (dest / "single_iface.json").exists()
    assert load_graph(dest / "single.json").base_commit == git(dest / "repo", "rev-parse", "HEAD")


def test_the_v2_task_cannot_be_the_original(tmp_path):
    tasks = make_task(tmp_path)
    with pytest.raises(ct.CleanError, match="cannot replace the original"):
        ct.clean_task("owner__pkg_1.0_1.1", tasks, tasks, use_docker=False)


def test_a_v2_task_is_graded_from_the_original_base(tmp_path):
    """sweevo.stage_code restores the patch's files from task_commit, so over the cleaned
    HEAD the test patch applies, and the graded files are those of the original task."""
    from aqours_code.taskgraph.sweevo import load_task, stage_code

    tasks = make_task(tmp_path)
    out = tmp_path / "v2"
    ct.clean_task("owner__pkg_1.0_1.1", tasks, out, use_docker=False)
    staged = {}
    for name, root in (("v1", tasks), ("v2", out)):
        task = load_task(root / "owner__pkg_1.0_1.1")
        final = tmp_path / f"{name}-final"
        gitops.export_commit(task.repo_dir, "HEAD", final)  # each version's starting code
        stage_code(final, tmp_path / f"{name}-scratch", task)
        staged[name] = {path.relative_to(tmp_path / f"{name}-scratch").as_posix():
                        path.read_bytes()
                        for path in (tmp_path / f"{name}-scratch").rglob("*")
                        if path.is_file() and ".git" not in path.parts}
        staged[f"{name}-head"] = git(tmp_path / f"{name}-scratch", "rev-parse", "HEAD")
    assert staged["v1"] == staged["v2"]
    assert staged["v1-head"] == staged["v2-head"]
    assert "def test_brand_new" in staged["v2"]["tests/test_mod.py"].decode()
