"""Planner v3: what the program fixes in the facts itself, and the narrow revision round."""
from __future__ import annotations

import copy

import pytest

from aqours_code.taskgraph.facts import (check_facts, normalize_facts_data, parse_facts,
                                         read_facts)
from aqours_code.taskgraph.planner import DraftError
from aqours_code.taskgraph.planner_v3 import (REVISION_INSTRUCTION, FactsRevision,
                                              build_facts_prompt)
from aqours_code.taskgraph.repo_index import RepoIndex
from aqours_code.taskgraph.validate import Issue
from test_planner import answer
from test_planner_v3 import ITEMS, TWO_UNITS, logic, plan_v3

# Units S and R of TWO_UNITS; S also edits README.md and gives it a "symbol".
WITH_DOC = copy.deepcopy(TWO_UNITS)
WITH_DOC["units"][0]["edits"].append(
    {"file": "README.md", "kind": "registration",
     "symbols": ["README.md::--cancel"], "lines": 1})
WITH_DOC["units"][0]["provides"].append("README.md::--cancel")
WITH_DOC["units"][1]["uses"] = [
    {"symbol": "README.md::--cancel", "need": "interface"}]


def index() -> RepoIndex:
    return RepoIndex(commit="abc", files={"store.py", "runner.py", "README.md",
                                          "tests/test_basic.py"},
                     symbols={"store.py::JobStore", "store.py::JobStore.add",
                              "runner.py::run_loop", "models.py::Job"})


def test_a_symbol_of_a_non_python_file_is_dropped_and_the_facts_pass():
    facts, fixes = read_facts(answer(WITH_DOC), index())
    s_unit = facts.units[0]
    script = next(edit for edit in s_unit.edits if edit.file == "README.md")
    assert script.symbols == [] and script.kind == "logic"  # a registration of nothing
    assert "README.md::--cancel" not in s_unit.provides
    assert facts.units[1].uses == []  # the uses entry went with its symbol
    assert check_facts(facts, ITEMS, index()) == []
    messages = [fix.message() for fix in fixes]
    assert ("unit S: edits[1].symbols 'README.md::--cancel' dropped (only Python "
            "files define symbols; the file stays in the unit)") in messages
    assert any(m.startswith("unit S: provides 'README.md::--cancel' dropped")
               for m in messages)
    assert any(m.startswith("unit R: uses 'README.md::--cancel' dropped")
               for m in messages)
    assert any(m.startswith("unit S: edits[1].kind 'registration' rewritten as 'logic'")
               for m in messages)
    with pytest.raises(DraftError):  # the strict parser still refuses it
        parse_facts(answer(WITH_DOC))


def test_a_registration_with_python_names_left_stays_a_registration():
    data = copy.deepcopy(TWO_UNITS)
    data["units"][0]["edits"].append(
        {"file": "store.py", "kind": "registration", "lines": 1,
         "symbols": ["store.py::JobStore.cancel", "store.py::JobStore.cancel"]})
    fixed, _ = normalize_facts_data(data)
    assert fixed["units"][0]["edits"][-1]["kind"] == "registration"
    assert fixed["units"][0]["edits"][-1]["symbols"] == ["store.py::JobStore.cancel"]


def test_other_meaning_preserving_fixes():
    data = copy.deepcopy(TWO_UNITS)
    s, r = data["units"]
    s["edits"][0]["file"] = ".\\store.py"
    s["edits"][0]["symbols"] = ["store.py::JobStore.cancel()"]
    s["provides"] = ["store.py::JobStore.cancel", "store.py::JobStore.cancel"]
    s["create"] = ["tests/test_cancel.py", "./tests/test_cancel.py"]
    r["uses"] = [{"symbol": "store.py::JobStore.cancel", "need": " Behaviour "}]
    fixed, fixes = normalize_facts_data(data)
    s2, r2 = fixed["units"]
    assert s2["edits"][0]["file"] == "store.py"
    assert s2["edits"][0]["symbols"] == ["store.py::JobStore.cancel"]
    assert s2["provides"] == ["store.py::JobStore.cancel"]
    assert s2["create"] == ["tests/test_cancel.py"]
    assert r2["uses"][0]["need"] == "behavior"
    assert {fix.action for fix in fixes} == {"rewritten", "dropped"}
    assert data["units"][0]["edits"][0]["file"] == ".\\store.py"  # the input is not changed


def test_a_use_of_an_existing_symbol_no_unit_changes_is_dropped():
    data = copy.deepcopy(TWO_UNITS)
    data["units"][1]["uses"] = [{"symbol": "models.py::Job", "need": "behavior"}]
    facts, fixes = read_facts(answer(data), index())
    assert facts.units[1].uses == []
    assert [(fix.unit, fix.field, fix.action) for fix in fixes] == [("R", "uses", "dropped")]
    assert check_facts(facts, ITEMS, index()) == []


def test_a_use_of_an_existing_symbol_in_an_edited_file_stays_an_error():
    # S edits store.py: it may change JobStore.add and have forgotten to provide it
    data = copy.deepcopy(TWO_UNITS)
    data["units"][1]["uses"] = [{"symbol": "store.py::JobStore.add", "need": "behavior"}]
    facts, fixes = read_facts(answer(data), index())
    assert fixes == []
    assert [issue.code for issue in check_facts(facts, ITEMS, index())] == ["F6"]


@pytest.mark.parametrize("change, code", [
    # a symbol in another file than its edit: the program cannot know which is right
    (lambda d: d["units"][0]["edits"][0]["symbols"].append("runner.py::run_loop"), "F3"),
    # a missing file
    (lambda d: d["units"][0]["edits"].append(logic("nowhere.py", "x")), "F3"),
    # an unknown need value
    (lambda d: d["units"][1].update(uses=[{"symbol": "store.py::JobStore.cancel",
                                           "need": "maybe"}]), "F6"),
    # an unknown unit in together
    (lambda d: d.update(together=[{"units": ["S", "Q"]}]), "F8"),
])
def test_meaning_changing_problems_are_not_fixed(change, code):
    data = copy.deepcopy(TWO_UNITS)
    change(data)
    facts, fixes = read_facts(answer(data), index())
    assert fixes == []
    assert code in [issue.code for issue in check_facts(facts, ITEMS, index())]


def test_a_malformed_python_symbol_is_still_a_format_error():
    data = copy.deepcopy(TWO_UNITS)
    data["units"][0]["provides"] = ["store.py::JobStore.can cel"]
    with pytest.raises(DraftError):
        read_facts(answer(data), index())


def test_the_plan_records_normalisations_and_needs_no_revision(toy_repo, tmp_path):
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(WITH_DOC)])
    report = result.report
    assert result.success and len(worker.requests) == 1  # no revision round
    assert report["revision_rounds"] == 0
    fixes = report["normalisations"]
    assert {fix["round"] for fix in fixes} == {1}
    assert any(fix["unit"] == "S" and fix["value"] == "README.md::--cancel"
               for fix in fixes)
    assert report["rounds"][0]["normalisations"] == [
        {key: value for key, value in fix.items() if key != "round"} for fix in fixes]


def test_a_meaning_changing_error_still_gets_a_revision(toy_repo, tmp_path):
    wrong = copy.deepcopy(TWO_UNITS)
    wrong["units"][0]["edits"][0]["symbols"].append("runner.py::run_loop")  # F3
    result, worker, _ = plan_v3(toy_repo, tmp_path, [answer(wrong), answer(TWO_UNITS)])
    assert result.success and len(worker.requests) == 2
    assert result.report["rounds"][0]["errors"][0]["code"] == "F3"
    second = worker.requests[1].prompt
    assert REVISION_INSTRUCTION in second
    assert "everything that the problems below do not name is fine" in second


def test_the_revision_prompt_is_narrow_and_truthful():
    index_ = RepoIndex(commit="abc", files={"store.py"})
    content = FactsRevision(1, 2, '{"units": []}', [Issue("F7", ["S"], "lists x in tests")])
    text = build_facts_prompt("Do it.", index_, ITEMS, "OUTLINE", content)
    assert REVISION_INSTRUCTION in text
    assert "Change only what these problems require" in text
    assert "read code again only when a problem cannot be fixed without it" in text
    assert "The rest of the answer passed the checks" in text
    assert '## Previous answer\n\n```json\n{"units": []}\n```' in text  # kept as before
    form = FactsRevision(1, 2, "{}", [Issue("FORMAT", [], "units[0].size: bad")])
    text = build_facts_prompt("Do it.", index_, ITEMS, "OUTLINE", form)
    assert "Only the format problems below were found" in text
    assert "The rest of the answer passed the checks" not in text  # F1-F9 did not run yet
