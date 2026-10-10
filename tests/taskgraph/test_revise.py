"""Step 3 of the planner: rule-based merging (revise.py), on hand-made graphs."""
from __future__ import annotations

from aqours_code.taskgraph.planner import planner_checks
from aqours_code.taskgraph.revise import (
    CONVENTIONS_HEADING,
    goal_with_conventions,
    revise_graph,
)
from aqours_code.taskgraph.schema import graph_to_json
from aqours_code.taskgraph.validate import validate
from taskgraph_support import make_edge, make_graph, make_node


def ids(graph) -> list[str]:
    return [node.id for node in graph.nodes]


def edges(graph) -> list[tuple[str, str, str]]:
    return [(edge.from_, edge.to, edge.type) for edge in graph.edges]


def node(graph, node_id):
    return next(n for n in graph.nodes if n.id == node_id)


def proposal_graph():
    """The example of the project proposal: C and D both edit runner.py."""
    return make_graph([
        make_node("A", kind="contract", modify=("models.py",),
                  provides=("models.py::Job.attempts",)),
        make_node("B", modify=("store.py",), requires=("models.py::Job.attempts",)),
        make_node("C", modify=("runner.py",), requires=("models.py::Job.attempts",)),
        make_node("D", modify=("runner.py",), requires=("models.py::Job.attempts",)),
        make_node("E", modify=("api.py",), requires=("models.py::Job.attempts",)),
        make_node("F", modify=("dashboard/page.py",), requires=("models.py::Job.attempts",)),
    ], [make_edge("A", "B", "interface"), make_edge("A", "C", "interface"),
        make_edge("A", "D", "interface"), make_edge("A", "E", "interface"),
        make_edge("A", "F", "interface"), make_edge("C", "D", "order")])


def test_proposal_example_merges_only_the_queued_pair():
    graph = proposal_graph()
    revised = revise_graph(graph)
    assert ids(revised.graph) == ["A", "B", "C_D", "E", "F"]
    assert edges(revised.graph) == [("A", "B", "interface"), ("A", "C_D", "interface"),
                                    ("A", "E", "interface"), ("A", "F", "interface")]
    merged = node(revised.graph, "C_D")
    assert merged.kind == "implement" and merged.edit_set.modify == ["runner.py"]
    assert merged.title == "C + D"
    assert [(m.rule, m.members, m.into, m.files) for m in revised.merges] == [
        ("M1", ["C", "D"], "C_D", ["runner.py"])]
    log = revised.graph.revision_log
    assert [(e.action, e.nodes, e.into) for e in log] == [("merge", ["C", "D"], "C_D")]
    assert log[0].reason == ("C and D both edit runner.py and can only run one after the "
                             "other (M1)")
    for untouched in "ABEF":
        assert node(revised.graph, untouched) == node(graph, untouched)
    assert validate(revised.graph).ok
    assert planner_checks(revised.graph) == []


def test_a_chain_on_one_file_becomes_one_node():
    graph = make_graph([make_node(name, modify=("runner.py",)) for name in "SRNH"],
                       [make_edge("S", "R", "order"), make_edge("R", "N", "order"),
                        make_edge("N", "H", "order")])
    revised = revise_graph(graph)
    assert ids(revised.graph) == ["S_R_N_H"] and revised.graph.edges == []
    assert [m.into for m in revised.merges] == ["S_R", "S_R_N", "S_R_N_H"]
    merged = revised.graph.nodes[0]
    assert merged.title == "S + R + N + H"
    assert merged.goal.startswith("This sub-task combines 4 parts. Do them all, in this order.")
    for number, name in enumerate("SRNH", 1):
        assert f"Part {number} ({name}: {name}):\ndo {name}" in merged.goal
    assert validate(revised.graph).ok


def test_a_node_with_another_unrelated_predecessor_is_not_merged():
    graph = make_graph([
        make_node("X", modify=("runner.py",)),
        make_node("Z", modify=("store.py",), provides=("store.py::JobStore.extra",)),
        make_node("Y", modify=("runner.py",), requires_impl=("store.py::JobStore.extra",)),
    ], [make_edge("X", "Y", "order"), make_edge("Z", "Y", "full")])
    revised = revise_graph(graph)
    assert ids(revised.graph) == ["X", "Z", "Y"] and revised.merges == []
    assert edges(revised.graph) == edges(graph)
    assert revised.not_merged == [{"rule": "M1", "nodes": ["X", "Y"], "files": ["runner.py"],
                                   "blocked_by": ["Z"]}]
    entry = revised.graph.revision_log[-1]
    assert (entry.action, entry.nodes) == ("other", ["X", "Y"])
    assert "were not merged (M1)" in entry.reason and "Z" in entry.reason


def test_a_predecessor_that_is_an_ancestor_of_the_first_node_allows_the_merge():
    graph = make_graph([
        make_node("Z", modify=("store.py",)),
        make_node("X", modify=("runner.py",)),
        make_node("Y", modify=("runner.py",)),
    ], [make_edge("Z", "X", "full"), make_edge("X", "Y", "order"),
        make_edge("Z", "Y", "full")])
    revised = revise_graph(graph)
    assert ids(revised.graph) == ["Z", "X_Y"]
    assert edges(revised.graph) == [("Z", "X_Y", "full")]
    assert revised.not_merged == []


def test_contract_with_one_downstream_node_is_merged_into_it():
    graph = make_graph([
        make_node("C", kind="contract", modify=("models.py",), create=("feature.py",),
                  provides=("feature.py::run",)),
        make_node("Y", modify=("feature.py",), create=("tests/test_feature.py",),
                  provides=("feature.py::run",), requires=("feature.py::run",),
                  context_files=("feature.py", "README.md")),
    ], [make_edge("C", "Y", "interface")])
    revised = revise_graph(graph)
    assert ids(revised.graph) == ["C_Y"] and revised.graph.edges == []
    merged = revised.graph.nodes[0]
    assert merged.kind == "implement"
    assert merged.edit_set.create == ["feature.py", "tests/test_feature.py"]
    assert merged.edit_set.modify == ["models.py"]
    assert merged.requires == [] and merged.context_files == ["README.md"]
    assert [(m.rule, m.members) for m in revised.merges] == [("M2", ["C", "Y"])]
    assert "(M2)" in revised.graph.revision_log[0].reason
    assert validate(revised.graph).ok


def test_contract_with_two_downstream_nodes_stays():
    graph = make_graph([
        make_node("C", kind="contract", modify=("models.py",)),
        make_node("P", modify=("store.py",)), make_node("Q", modify=("runner.py",)),
    ], [make_edge("C", "P", "interface"), make_edge("C", "Q", "interface")])
    revised = revise_graph(graph)
    assert ids(revised.graph) == ["C", "P", "Q"] and revised.merges == []
    assert revised.graph == graph


def test_contract_is_merged_after_its_downstream_nodes_were_merged():
    graph = make_graph([
        make_node("C", kind="contract", modify=("models.py",)),
        make_node("P", modify=("runner.py",)), make_node("Q", modify=("runner.py",)),
    ], [make_edge("C", "P", "interface"), make_edge("C", "Q", "interface"),
        make_edge("P", "Q", "order")])
    revised = revise_graph(graph)
    assert [(m.rule, m.into) for m in revised.merges] == [("M1", "P_Q"), ("M2", "C_P_Q")]
    assert ids(revised.graph) == ["C_P_Q"]
    assert revised.graph.nodes[0].goal.startswith("This sub-task combines 3 parts.")
    assert validate(revised.graph).ok


def test_modular_shape_is_left_alone():
    features = ["priority", "recurring", "dependencies", "limits", "notify", "audit"]
    graph = make_graph(
        [make_node("C", kind="contract", modify=("models.py",),
                   create=tuple(f"{name}.py" for name in features))]
        + [make_node(name.upper(), modify=(f"{name}.py",),
                     create=(f"tests/test_{name}.py",)) for name in features],
        [make_edge("C", name.upper(), "interface") for name in features])
    revised = revise_graph(graph)
    assert revised.graph == graph
    assert revised.merges == [] and revised.entries == [] and revised.not_merged == []


def test_fields_of_a_merged_node():
    conventions = ["Take `now` from the caller.", "Use JobStore."]
    first = make_node(
        "X", modify=("runner.py", "store.py"), create=("helper.py",),
        provides=("helper.py::assist", "runner.py::run_loop"),
        requires=("models.py::Job", "store.py::JobStore.jobs"),
        requires_impl=("models.py::JobStatus",), symbols=("runner.py::run_loop",),
        commands=("check-x", "check-shared"), context_files=("README.md", "models.py"))
    second = make_node(
        "Y", modify=("runner.py", "helper.py"), create=("tests/test_y.py",),
        provides=("runner.py::MAX_ATTEMPTS",),
        requires=("helper.py::assist", "models.py::JobStatus", "models.py::Config"),
        requires_impl=("store.py::JobStore.jobs", "runner.py::run_loop"),
        symbols=("runner.py::MAX_ATTEMPTS",), commands=("check-shared", "check-y"),
        context_files=("helper.py", "models.py", "SPEC.md#Running"))
    first["check"]["timeout_s"] = 120
    second["check"]["timeout_s"] = 900
    for item in (first, second):
        item["goal"] = goal_with_conventions(item["goal"], conventions)
    other = make_node("Z", modify=("README.md",))
    graph = make_graph([other, first, second],
                       [make_edge("X", "Y", "full"), make_edge("X", "Z", "order"),
                        make_edge("Y", "Z", "full")])
    revised = revise_graph(graph, conventions)
    assert ids(revised.graph) == ["Z", "X_Y"]          # at the first member's position
    assert edges(revised.graph) == [("X_Y", "Z", "full")]  # the strongest of order and full
    merged = node(revised.graph, "X_Y")
    assert merged.edit_set.create == ["helper.py", "tests/test_y.py"]
    assert merged.edit_set.modify == ["runner.py", "store.py"]     # helper.py is created
    assert merged.edit_set.symbols == ["runner.py::run_loop", "runner.py::MAX_ATTEMPTS"]
    assert merged.provides == ["helper.py::assist", "runner.py::run_loop",
                               "runner.py::MAX_ATTEMPTS"]
    # provided by a member: helper.py::assist, runner.py::run_loop; in both lists:
    # models.py::JobStatus and store.py::JobStore.jobs stay only in requires_impl
    assert merged.requires == ["models.py::Job", "models.py::Config"]
    assert merged.requires_impl == ["models.py::JobStatus", "store.py::JobStore.jobs"]
    assert merged.check.commands == ["check-x", "check-shared", "check-y"]
    assert merged.check.timeout_s == 900
    assert merged.context_files == ["README.md", "models.py", "SPEC.md#Running"]
    assert merged.goal.count(CONVENTIONS_HEADING) == 1
    assert merged.goal == (
        "This sub-task combines 2 parts. Do them all, in this order.\n\n"
        "Part 1 (X: X):\ndo X\n\nPart 2 (Y: Y):\ndo Y\n\n"
        "Repository conventions:\n- Take `now` from the caller.\n- Use JobStore.")
    assert node(revised.graph, "Z").goal == "do Z"


def test_id_collision_gets_a_suffix():
    graph = make_graph([make_node("A", modify=("runner.py",)),
                        make_node("B", modify=("runner.py",)),
                        make_node("A_B", modify=("store.py",))],
                       [make_edge("A", "B", "order")])
    assert ids(revise_graph(graph).graph) == ["A_B_2", "A_B"]


def test_revision_is_deterministic():
    first = revise_graph(proposal_graph())
    second = revise_graph(proposal_graph())
    assert graph_to_json(first.graph) == graph_to_json(second.graph)
    assert first.merges == second.merges
    chain = make_graph([make_node(name, modify=("runner.py",)) for name in "SRNH"],
                       [make_edge("S", "R", "order"), make_edge("R", "N", "order"),
                        make_edge("N", "H", "order")])
    assert graph_to_json(revise_graph(chain).graph) == graph_to_json(revise_graph(chain).graph)
