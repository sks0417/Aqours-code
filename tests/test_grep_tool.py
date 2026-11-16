"""The read-only grep tool (``run_grep``): matches, limits, skipped files, refusals."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aqours_code import bootstrap

bootstrap()

from aqours_code.tool_handlers import run_grep  # noqa: E402


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "ws"
    write(root, "pkg/core.py", "import os\n\n\nclass Frame:\n    def merge(self, other):\n"
                               "        return other\n")
    write(root, "pkg/util.py", "def merge(a, b):\n    return a\n")
    write(root, "README.md", "Merge frames with merge().\n")
    return root


def grep(workspace: Path, pattern: str, **kwargs) -> str:
    return run_grep(pattern, cwd=workspace, **kwargs)


# ── matches ──

def test_matching_lines_with_relative_path_and_line_number(workspace):
    out = grep(workspace, r"def merge")
    assert out.splitlines() == [
        "pkg/core.py:5:     def merge(self, other):",
        "pkg/util.py:1: def merge(a, b):",
    ]


def test_path_glob_and_ignore_case(workspace):
    assert grep(workspace, "merge", path="pkg/util.py") == "pkg/util.py:1: def merge(a, b):"
    assert grep(workspace, "merge", path="pkg", glob="core.py").splitlines() == [
        "pkg/core.py:5:     def merge(self, other):"]
    assert grep(workspace, "^Merge", glob="*.md") == "README.md:1: Merge frames with merge()."
    assert grep(workspace, "^merge", glob="*.md") == "no matches"
    assert grep(workspace, "^merge", glob="*.md", ignore_case=True).startswith("README.md:1:")
    assert grep(workspace, "merge", glob="pkg/*.py").count("\n") == 1  # two lines


def test_no_matches_is_said_plainly(workspace):
    assert grep(workspace, "no_such_name_anywhere") == "no matches"


def test_an_invalid_regular_expression_is_an_error(workspace):
    out = grep(workspace, "def (merge")
    assert out.startswith("Error: invalid regular expression 'def (merge'")
    assert "missing )" in out


def test_bad_arguments_are_errors_not_exceptions(workspace):
    assert grep(workspace, "") == "Error: pattern must not be empty"
    assert run_grep(None, cwd=workspace) == "Error: pattern must not be empty"
    assert grep(workspace, "x", max_results="many").startswith("Error: max_results")
    assert grep(workspace, "x", path="missing").startswith("Error: path not found")


# ── limits ──

def test_max_results_defaults_to_100_and_reports_the_rest(workspace):
    write(workspace, "many.txt", "hit\n" * 130)
    lines = grep(workspace, "hit").splitlines()
    assert len(lines) == 101
    assert lines[0] == "many.txt:1: hit" and lines[99] == "many.txt:100: hit"
    assert lines[-1].startswith("... 30 more matches not shown (max_results=100)")


def test_max_results_is_capped_at_300(workspace):
    write(workspace, "many.txt", "hit\n" * 400)
    lines = grep(workspace, "hit", max_results=1000).splitlines()
    assert len(lines) == 301
    assert lines[-1].startswith("... 100 more matches not shown (max_results=300)")
    assert len(grep(workspace, "hit", max_results=5).splitlines()) == 6
    assert len(grep(workspace, "hit", max_results=0).splitlines()) == 2  # at least one


def test_a_long_line_is_cut_at_200_characters(workspace):
    write(workspace, "long.py", "x = '" + "a" * 500 + "'\n")
    out = grep(workspace, "x = ", path="long.py")
    content = out.split(": ", 1)[1]
    assert content == ("x = '" + "a" * 195) + " ...(line truncated)"


def test_total_output_stops_near_8000_characters(workspace):
    write(workspace, "wide.txt", ("hit " + "b" * 180 + "\n") * 80)
    lines = grep(workspace, "hit").splitlines()
    shown = lines[:-1]
    assert sum(len(line) + 1 for line in shown) <= 8000
    assert len(shown) < 80
    assert lines[-1].startswith(f"... {80 - len(shown)} more matches not shown "
                                "(output limit of 8000 characters)")


# ── skipped files ──

def test_skips_git_binary_and_large_files(workspace):
    write(workspace, ".git/config", "needle\n")
    write(workspace, "sub/.git", "gitdir: needle\n")  # a worktree's .git file
    (workspace / "blob.bin").write_bytes(b"\x00\x01needle\n")
    (workspace / "big.txt").write_bytes(b"needle\n" + b"z" * (2 * 1024 * 1024))
    write(workspace, "small.txt", "needle\n")
    assert grep(workspace, "needle") == "small.txt:1: needle"
    assert "binary or larger than 2 MB" in grep(workspace, "needle", path="blob.bin")
    assert "binary or larger than 2 MB" in grep(workspace, "needle", path="big.txt")


# ── the workspace boundary ──

def test_a_path_outside_the_workspace_is_refused(workspace, tmp_path):
    write(tmp_path, "outside/secret.txt", "needle\n")
    for path in ("..", "../outside", "pkg/../../outside", str(tmp_path / "outside")):
        out = grep(workspace, "needle", path=path)
        assert out == f"Error: Path escapes workspace: {path}", path
    # an absolute path inside the workspace is allowed, as for read_file
    assert grep(workspace, "def merge", path=str(workspace / "pkg" / "util.py")) == (
        "pkg/util.py:1: def merge(a, b):")


def test_files_behind_a_link_to_outside_are_not_searched(workspace, tmp_path):
    write(tmp_path, "outside/secret.txt", "needle\n")
    link = workspace / "link"
    try:
        os.symlink(tmp_path / "outside", link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("cannot create a symlink here")
        import _winapi  # noqa: PLC0415 - CPython on Windows
        _winapi.CreateJunction(str(tmp_path / "outside"), str(link))
    assert grep(workspace, "needle") == "no matches"
    assert grep(workspace, "needle", path="link").startswith("Error: Path escapes workspace")


def test_the_runtime_workspace_is_the_default_root(workspace):
    from types import SimpleNamespace

    runtime = SimpleNamespace(paths=SimpleNamespace(workdir=workspace))
    assert run_grep("def merge", path="pkg/util.py", runtime=runtime) == (
        "pkg/util.py:1: def merge(a, b):")
