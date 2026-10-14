"""``resynth``: a v3 graph built again from the facts saved with it, without a model."""
from __future__ import annotations

import json

import pytest

from aqours_code.taskgraph import build_index, load_graph
from aqours_code.taskgraph.affected import scan_test_imports
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.facts import Facts
from aqours_code.taskgraph.planner import PLANNER_VERSION, GraphInfo, report_path_for
from aqours_code.taskgraph.planner_v3 import ResynthError, run_resynth
from aqours_code.taskgraph.schema import dump_graph, graph_to_json
from aqours_code.taskgraph.single import single_graph
from aqours_code.taskgraph.synthesize import synthesize

FINAL = "python -m pytest -q tests"
REQUEST = "Add job cancellation to the toy queue.\n"


def logic(path: str, *symbols: str, lines: int = 30) -> dict:
    return {"file": path, "kind": "logic", "symbols": [f"{path}::{s}" for s in symbols],
            "lines": lines}


FACTS = {"conventions": ["All storage access goes through JobStore."], "units": [
    {"id": "S", "item": "1", "title": "cancel", "goal": "goal S", "size": 120,
     "edits": [logic("store.py", "JobStore.cancel")], "create": ["tests/test_cancel.py"],
     "provides": ["store.py::JobStore.cancel"], "tests": ["tests/test_cancel.py"]},
    {"id": "R", "item": "2", "title": "skip", "goal": "goal R", "size": 120,
     "edits": [logic("runner.py", "run_loop")], "create": ["tests/test_skip.py"],
     "provides": ["runner.py::run_loop"],
     "uses": [{"symbol": "store.py::JobStore.cancel", "need": "interface"}],
     "tests": ["tests/test_skip.py"]},
]}


def saved_plan(toy_repo, tmp_path, facts: dict = FACTS) -> tuple:
    """A graph and a report written by hand, as ``plan`` would have left them."""
    graph = single_graph(REQUEST, toy_repo.path, [FINAL]).model_copy(update={
        "repo": str(toy_repo.path), "request_id": "toy-v3"})
    graph = graph.model_copy(update={"generator": graph.generator.model_copy(update={
        "kind": "planner", "planner_version": "planner-v3", "model": "some-model",
        "revision_mode": "none"})})
    path = tmp_path / "old" / "graph.json"
    path.parent.mkdir()
    dump_graph(graph, path)
    report = {"planner": "v3", "request": "request.md", "facts": facts,
              "draft_items": [{"id": "1", "title": "cancel", "description": ""},
                              {"id": "2", "title": "skip", "description": ""}],
              "estimated_changed_lines": 900,
              "fast_path": {"taken": False, "estimated_changed_lines": 900,
                            "threshold": 500}}
    report_path_for(path).write_text(json.dumps(report), encoding="utf-8")
    return graph, path


def direct(toy_repo, facts: dict = FACTS, **kw):
    index = build_index(toy_repo.path, toy_repo.commit)
    info = GraphInfo("toy-v3", REQUEST, str(toy_repo.path.resolve()), toy_repo.commit,
                     [FINAL], "some-model")
    return synthesize(Facts.model_validate(facts), index, info,
                      scan_test_imports(toy_repo.path, index), **kw)


def test_resynth_gives_the_graph_synthesize_gives(toy_repo, tmp_path):
    _, path = saved_plan(toy_repo, tmp_path)
    out = tmp_path / "new" / "graph.json"
    result = run_resynth(path, out)
    assert result.success
    expected = direct(toy_repo)
    assert graph_to_json(load_graph(out)) == graph_to_json(expected.graph)
    assert load_graph(out).generator.planner_version == PLANNER_VERSION
    report = json.loads(report_path_for(out).read_text(encoding="utf-8"))
    assert report["resynth_of"] == str(path)
    assert report["decisions"] == [d.to_dict() for d in expected.decisions]
    assert report["facts"]["units"][0]["id"] == "S" and report["model"] == "some-model"
    assert report["totals"] == {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
    assert report["rounds"] == [] and report["stage1"] == []
    assert report["draft_items"][0]["id"] == "1"
    assert report["planner"] == "v3" and report["request"] == "request.md"


def test_resynth_applies_ablation(toy_repo, tmp_path):
    _, path = saved_plan(toy_repo, tmp_path)
    out = tmp_path / "new" / "graph.json"
    result = run_resynth(path, out, frozenset({"p4"}))
    assert result.success
    graph = load_graph(out)
    assert graph.generator.ablate == ["p4"]
    assert graph_to_json(graph) == graph_to_json(direct(toy_repo, ablate={"p4"}).graph)
    assert result.report["ablate"] == ["p4"]


def test_resynth_command_prints_the_plan_summary(toy_repo, tmp_path, capsys):
    _, path = saved_plan(toy_repo, tmp_path)
    out = tmp_path / "new" / "graph.json"
    assert main(["resynth", str(path), "--out", str(out), "--ablate", "p2"]) == 0
    output = capsys.readouterr().out
    assert f"synthesized again from the facts of {path} (no model calls)" in output
    # p2 off: R waits for S, a chain of all the work, so one agent does it
    assert "nodes: 1  edges: 0" in output and "ablated: p2" in output
    assert ("parallelism: critical path 240 / 240 lines (1.00 > 0.70) -> single agent"
            in output)
    assert "model calls: 0" in output and f"wrote {out}" in output


def test_resynth_refuses_inputs_without_facts(toy_repo, tmp_path, capsys):
    graph, path = saved_plan(toy_repo, tmp_path)
    out = tmp_path / "new" / "graph.json"
    report_path_for(path).write_text(json.dumps({"planner_version": "planner-v2"}),
                                     encoding="utf-8")
    with pytest.raises(ResynthError, match="has no facts"):
        run_resynth(path, out)
    report_path_for(path).unlink()
    assert main(["resynth", str(path), "--out", str(out)]) == 2
    assert "plan report not found" in capsys.readouterr().err
    assert main(["resynth", str(path), "--out", str(out), "--ablate", "p7"]) == 2


def test_resynth_reports_facts_that_no_longer_pass_the_checks(toy_repo, tmp_path):
    broken = json.loads(json.dumps(FACTS))
    broken["units"][1]["uses"][0]["symbol"] = "store.py::JobStore.nope"
    _, path = saved_plan(toy_repo, tmp_path, broken)
    out = tmp_path / "new" / "graph.json"
    result = run_resynth(path, out)
    assert not result.success and not out.exists()
    assert [issue["code"] for issue in result.report["errors"]] == ["F6"]


def test_resynth_reads_facts_saved_before_why_existed(toy_repo, tmp_path):
    old = json.loads(json.dumps(FACTS))
    old["units"][1]["uses"][0]["need"] = "behavior"  # no "why": written by planner-v3
    assert all("why" not in use for unit in old["units"] for use in unit.get("uses", []))
    _, path = saved_plan(toy_repo, tmp_path, old)
    result = run_resynth(path, tmp_path / "new" / "graph.json")
    assert result.success
    assert result.report["behavior_without_why"] == [
        {"unit": "R", "symbol": "store.py::JobStore.cancel"}]
