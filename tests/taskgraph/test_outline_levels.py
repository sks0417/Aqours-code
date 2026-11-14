"""The two-level outline of the planner: level 1, the choice of level 2, the prompt, the report."""
from __future__ import annotations

from pathlib import Path

import pytest

from aqours_code.taskgraph.affected import scan_sources
from aqours_code.taskgraph.outline_levels import (
    COMMON_DEFINED_IN,
    FULL_OUTLINE_PARAGRAPH,
    TWO_LEVEL_PARAGRAPH,
    build_two_level_outline,
    choose_level2,
    level1_line,
    module_name,
    other_files_list,
    two_level_instructions,
)
from aqours_code.taskgraph.outline import _read
from aqours_code.taskgraph.planner import load_prompt
from aqours_code.taskgraph.planner_v3 import build_facts_prompt, load_facts_prompt
from aqours_code.taskgraph.repo_index import RepoIndex
from test_planner_v3 import ITEMS, TWO_UNITS, plan_v3
from test_planner import answer

FILES = {
    "pkg/__init__.py": "from pkg.core import Engine\n",
    "pkg/core.py": ("from pkg.util import helper\n\n\nclass Engine:\n    rows = 1\n\n"
                    "    def start(self, speed: int = 1) -> None:\n        pass\n\n"
                    "    def _private(self):\n        pass\n\n\ndef make_engine():\n"
                    "    return Engine()\n"),
    "pkg/util.py": "def helper(x):\n    return x\n\n\ndef parse_query_string(text):\n    return text\n",
    "pkg/remote.py": "from pkg.core import Engine\n\n\nclass Remote:\n    def error(self):\n        pass\n",
    "pkg/broken.py": "def broken(:\n",
    "pkg/plugins/a.py": "def run():\n    pass\n",
    "pkg/plugins/b.py": "def run():\n    pass\n",
    "pkg/plugins/c.py": "def run():\n    pass\n",
    "pkg/plugins/d.py": "def run():\n    pass\n",
    "tests/test_core.py": "from pkg.core import Engine\n",
    "README.md": "# pkg\n",
}


@pytest.fixture
def repo(tmp_path) -> tuple[Path, RepoIndex]:
    for relative, content in FILES.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(content, encoding="utf-8", newline="\n")
    return tmp_path, RepoIndex(commit="abc", files=set(FILES))


def source_files(root: Path, index: RepoIndex):
    from aqours_code.taskgraph.outline import is_source_file

    return [_read(root, path) for path in sorted(index.files) if is_source_file(path)]


def imports_of(root: Path, index: RepoIndex):
    return scan_sources({path: (root / path).read_text(encoding="utf-8")
                         for path in index.files if path.endswith(".py")}, index.files)


def chosen(request: str, root: Path, index: RepoIndex) -> list[tuple[str, str]]:
    return [(item.path, item.rule)
            for item in choose_level2(request, source_files(root, index), imports_of(root, index))]


def test_level1_names_top_level_classes_and_functions_only(repo):
    root, index = repo
    files = {file.path: file for file in source_files(root, index)}
    assert level1_line(files["pkg/core.py"]) == "- pkg/core.py (15 lines): Engine, make_engine()"
    assert level1_line(files["pkg/broken.py"]) == "- pkg/broken.py (1 lines): (could not be parsed)"
    assert level1_line(files["pkg/__init__.py"]) == "- pkg/__init__.py (1 lines)"


def test_module_names():
    assert module_name("dvc/remote/local.py") == "dvc.remote.local"
    assert module_name("dvc/remote/__init__.py") == "dvc.remote"
    assert module_name("src/click/types.py") == "click.types"


def test_rule_a_paths_and_dotted_module_names(repo):
    root, index = repo
    assert chosen("Change `pkg/remote.py` and pkg.util.", root, index)[:2] == [
        ("pkg/remote.py", "a"), ("pkg/util.py", "a")]
    # a one-part module name is a word, not a file: "pkg" does not pick pkg/__init__.py
    assert ("pkg/__init__.py", "a") not in chosen("Fix pkg so that it works.", root, index)
    assert chosen("see mypkg/remote.py", root, index) == []  # not a path of this repository


def test_rule_b_names_and_what_counts_as_common(repo):
    root, index = repo
    found = dict(chosen("Make parse_query_string faster and give Engine a new mode.",
                        root, index))
    assert found["pkg/util.py"] == "b" and found["pkg/core.py"] == "b"
    # a plain lowercase word counts only inside an inline code span
    assert "pkg/remote.py" not in dict(chosen("Show the error to the user.", root, index))
    assert dict(chosen("Call `error` once.", root, index))["pkg/remote.py"] == "b"
    fenced = "Interface:\n\n```python\ndef error(self):\n    ...\n```\n"
    assert "pkg/remote.py" not in dict(chosen(fenced, root, index))
    # defined in more than COMMON_DEFINED_IN files, or shorter than 4 characters: left out
    assert COMMON_DEFINED_IN == 3
    assert chosen("Call `run` in every plugin.", root, index) == []


def test_rule_c_import_neighbours_both_ways(repo):
    root, index = repo
    found = chosen("Change pkg/core.py.", root, index)
    assert found[0] == ("pkg/core.py", "a")
    neighbours = {path for path, rule in found if rule == "c"}
    # core imports util; __init__ and remote import core; tests are not source files
    assert neighbours == {"pkg/util.py", "pkg/__init__.py", "pkg/remote.py"}


def test_the_budget_cuts_and_reports(repo):
    root, index = repo
    outline = build_two_level_outline(root, index, "Change pkg/core.py and pkg/util.py.",
                                      imports_of(root, index), budget=120)
    kept = [item.path for item in outline.chosen]
    cut = [item.path for item in outline.cut]
    assert "pkg/core.py" in cut  # too big for 120 characters
    assert "pkg/util.py" in kept  # smaller files after it still fit
    assert outline.level2_chars <= 120
    report = outline.report()
    assert report["outline_level2_cut"][0]["rule"] in ("a", "c")
    assert {"path", "rule", "why", "chars"} <= set(report["outline_level2_files"][0])


def test_the_outline_text(repo):
    root, index = repo
    outline = build_two_level_outline(root, index, "Change pkg/core.py.", imports_of(root, index))
    text = outline.text
    assert "## Level 1: every source file\n\n- pkg/__init__.py (1 lines)" in text
    assert "- pkg/plugins/a.py (2 lines): run()" in text
    assert "## Level 2: details of some files\n\n### pkg/core.py (15 lines)" in text
    assert "  def start(self, speed: int=1) -> None (L7)" in text  # signatures, lines
    assert "fields: rows" in text and "def _private(self)" in text
    assert "### pkg/plugins/a.py" not in text  # level 1 only
    assert outline.level1_chars > 0 and outline.files == 9


def test_other_files_are_listed_and_crowded_directories_collapse(tmp_path):
    files = {f"tests/data/case{n}.json" for n in range(20)} | {"tests/test_a.py", "README.md",
                                                               "pkg/core.py"}
    text = other_files_list(RepoIndex(commit="x", files=files))
    assert "- README.md" in text and "- tests/test_a.py" in text
    assert "- tests/data/ (20 files: 20 .json; list them with glob)" in text
    assert "pkg/core.py" not in text  # a source file: it is in the outline


def test_instructions_swap_the_outline_paragraph():
    facts = load_facts_prompt()
    assert FULL_OUTLINE_PARAGRAPH in facts  # the swap below depends on it
    two = load_facts_prompt("two-level")
    assert TWO_LEVEL_PARAGRAPH in two and FULL_OUTLINE_PARAGRAPH not in two
    assert two.replace(TWO_LEVEL_PARAGRAPH, FULL_OUTLINE_PARAGRAPH) == facts
    with pytest.raises(ValueError):
        two_level_instructions(load_prompt().replace(FULL_OUTLINE_PARAGRAPH[:40], "x"))


def test_the_facts_prompt_in_both_modes(repo):
    root, index = repo
    full = build_facts_prompt("Change it.", index, ITEMS, "OUTLINE")
    assert full == build_facts_prompt("Change it.", index, ITEMS, "OUTLINE", outline_mode="full")
    assert "Its files:\n\n- README.md\n- pkg/__init__.py" in full
    two = build_facts_prompt("Change it.", index, ITEMS, "OUTLINE", outline_mode="two-level")
    assert ("Its source files are in the outline below; its other files:\n\n- README.md\n"
            "- tests/test_core.py") in two
    assert "pkg/core.py" not in two.split("# Repository outline")[0].split("# Repository")[1]
    assert TWO_LEVEL_PARAGRAPH in two


def test_run_plan_records_the_outline_mode(toy_repo, tmp_path):
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(TWO_UNITS)])
    report = result.report
    assert report["outline_mode"] == "two-level" and report["outline_reduction"] is None
    assert report["outline_level1_chars"] > 0 and report["file_list_chars"] > 0
    assert isinstance(report["outline_level2_files"], list)
    assert isinstance(report["outline_level2_cut"], list)
    prompt = worker.requests[0].prompt
    assert "## Level 1: every source file" in prompt and TWO_LEVEL_PARAGRAPH in prompt

    (tmp_path / "full").mkdir()
    full, worker, _ = plan_v3(toy_repo, tmp_path / "full", [answer(TWO_UNITS)], outline="full")
    assert full.report["outline_mode"] == "full"
    assert full.report["outline_level1_chars"] is None
    assert full.report["outline_reduction"] == "none"
    assert FULL_OUTLINE_PARAGRAPH in worker.requests[0].prompt
    assert "## Level 1" not in worker.requests[0].prompt
