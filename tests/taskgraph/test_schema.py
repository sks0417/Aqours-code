from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from aqours_code.taskgraph import Graph, dump_graph, load_graph, parse_symbol
from aqours_code.taskgraph.schema import (
    DEFAULT_SCHEMA_PATH,
    Edge,
    graph_json_schema,
    graph_to_json,
    validate_repo_path,
)
from taskgraph_support import EXAMPLE_GRAPH


@pytest.fixture
def example_data() -> dict:
    return json.loads(EXAMPLE_GRAPH.read_text(encoding="utf-8"))


def _invalid(data: dict) -> None:
    with pytest.raises(ValidationError):
        Graph.model_validate(data)


def test_example_graph_loads():
    graph = load_graph(EXAMPLE_GRAPH)
    assert len(graph.nodes) == 4
    assert {edge.type for edge in graph.edges} == {"interface", "full", "order"}
    assert graph.nodes[1].check.timeout_s == 300
    assert graph.edges[0].from_ == "status-failed"


@pytest.mark.parametrize("path", [
    ("request_id",),
    ("generator",),
    ("nodes",),
    ("nodes", 0, "goal"),
    ("nodes", 0, "check"),
    ("nodes", 0, "edit_set", "modify"),
    ("nodes", 0, "check", "commands"),
    ("edges", 0, "reason"),
    ("edges", 0, "from"),
])
def test_missing_required_field_is_rejected(example_data, path):
    data = copy.deepcopy(example_data)
    target = data
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    _invalid(data)


@pytest.mark.parametrize("location", ["top", "node", "edit_set", "check", "edge", "generator"])
def test_unknown_field_is_rejected(example_data, location):
    data = copy.deepcopy(example_data)
    target = {
        "top": data,
        "node": data["nodes"][0],
        "edit_set": data["nodes"][0]["edit_set"],
        "check": data["nodes"][0]["check"],
        "edge": data["edges"][0],
        "generator": data["generator"],
    }[location]
    target["unexpected"] = 1
    _invalid(data)


def test_misspelled_field_is_rejected(example_data):
    data = copy.deepcopy(example_data)
    data["nodes"][0]["require"] = data["nodes"][0].pop("provides")
    _invalid(data)


@pytest.mark.parametrize("path,value", [
    (("nodes", 0, "kind"), "bogus"),
    (("nodes", 0, "size"), "huge"),
    (("edges", 0, "type"), "weak"),
    (("edges", 0, "source"), "human"),
    (("generator", "kind"), "auto"),
    (("generator", "revision_mode"), "manual"),
])
def test_invalid_enum_is_rejected(example_data, path, value):
    data = copy.deepcopy(example_data)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _invalid(data)


@pytest.mark.parametrize("bad", [
    "/abs/models.py", "../models.py", "src/../models.py", "./models.py",
    "src//models.py", "src/", "C:/models.py", "src\\models.py", "",
])
@pytest.mark.parametrize("field", ["modify", "create", "context_files"])
def test_invalid_paths_are_rejected(example_data, bad, field):
    data = copy.deepcopy(example_data)
    node = data["nodes"][0]
    if field == "context_files":
        node["context_files"] = [bad]
    else:
        node["edit_set"][field] = [bad]
    _invalid(data)


@pytest.mark.parametrize("good", ["models.py", "src/pkg/mod.py", "tests/test_x.py", ".github/ci.yml"])
def test_valid_paths_are_accepted(good):
    assert validate_repo_path(good) == good


@pytest.mark.parametrize("bad", [
    "models.py:JobStatus", "models.py::", "::JobStatus", "models.py::1abc",
    "models.py::JobStatus..FAILED", "models.py::JobStatus.", "/models.py::JobStatus",
    "../models.py::JobStatus", "a.py::b::c", "models.py::Job Status", "models.py",
])
@pytest.mark.parametrize("field", ["requires", "provides", "symbols"])
def test_invalid_symbols_are_rejected(example_data, bad, field):
    data = copy.deepcopy(example_data)
    node = data["nodes"][1]
    if field == "symbols":
        node["edit_set"]["symbols"] = [bad]
    else:
        node[field] = [bad]
    _invalid(data)


def test_parse_symbol_splits_path_and_qualified_name():
    ref = parse_symbol("store.py::JobStore.list_unfinished")
    assert ref.path == "store.py"
    assert ref.qualname == "JobStore.list_unfinished"
    assert ref.parts == ("JobStore", "list_unfinished")
    assert str(ref) == "store.py::JobStore.list_unfinished"
    assert parse_symbol("pkg/runner.py::run_loop").path == "pkg/runner.py"
    with pytest.raises(ValueError):
        parse_symbol("runner.py:run_loop")


@pytest.mark.parametrize("path,value", [
    (("nodes", 0, "check", "timeout_s"), 0),
    (("nodes", 0, "check", "timeout_s"), -5),
    (("nodes", 0, "check", "timeout_s"), "60"),
    (("nodes", 0, "goal"), ""),
    (("nodes", 0, "goal"), "   "),
    (("nodes", 0, "id"), "has space"),
    (("nodes", 0, "id"), "dot.ted"),
    (("nodes", 0, "id"), ""),
    (("nodes",), []),
])
def test_invalid_values_are_rejected(example_data, path, value):
    data = copy.deepcopy(example_data)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _invalid(data)


def test_edge_accepts_python_field_name_and_serializes_alias():
    edge = Edge(from_="a", to="b", type="order", source="derived", reason="r")
    assert edge.model_dump(by_alias=True)["from"] == "a"


def test_json_round_trip_is_stable(tmp_path):
    graph = load_graph(EXAMPLE_GRAPH)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    dump_graph(graph, first)
    reloaded = load_graph(first)
    dump_graph(reloaded, second)
    assert reloaded == graph
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    assert '"from": "status-failed"' in graph_to_json(graph)


def test_round_trip_preserves_example_content(example_data):
    graph = Graph.model_validate(example_data)
    dumped = json.loads(graph_to_json(graph))
    # Defaults (e.g. timeout_s) are written out; everything given is preserved.
    for original, written in zip(example_data["nodes"], dumped["nodes"]):
        for key, value in original.items():
            if key == "check":
                assert written["check"]["commands"] == value["commands"]
            elif key == "edit_set":
                for sub_key, sub_value in value.items():
                    assert written["edit_set"][sub_key] == sub_value
            else:
                assert written[key] == value
    assert dumped["edges"] == example_data["edges"]


def test_committed_json_schema_is_up_to_date():
    committed = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert committed == graph_json_schema()
    edge_properties = committed["$defs"]["Edge"]["properties"]
    assert "from" in edge_properties and "from_" not in edge_properties
