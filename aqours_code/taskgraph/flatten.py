"""Flatten a task graph into one implement node that carries the whole plan.

``flatten_graph`` turns any valid graph into a single-node graph whose goal
lists every original node as a step, in topological order. Running it as a
single worker gives a "single agent with a detailed plan" baseline: the same
plan as the graph, without the parallelism or the division of work.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

from .schema import Check, EditSet, Graph, Node, RevisionEntry
from .validate import unique_nodes, usable_edges

PLANNED_ID = "planned"
STEPS_INTRO = "Work through the following steps in order; each step lists its goal and files."
SIZES = ("small", "medium", "large")


def topological_order(graph: Graph) -> list[Node]:
    """Nodes in dependency order; among ready nodes, the earlier in ``nodes`` first.

    Raises ``ValueError`` if the graph has a cycle.
    """
    nodes = unique_nodes(graph)
    position = {node.id: index for index, node in enumerate(nodes)}
    successors: dict[str, set[str]] = {node.id: set() for node in nodes}
    waiting = {node.id: 0 for node in nodes}
    for edge in usable_edges(graph):
        if edge.to not in successors[edge.from_]:
            successors[edge.from_].add(edge.to)
            waiting[edge.to] += 1
    ready = [node.id for node in nodes if waiting[node.id] == 0]
    order: list[Node] = []
    while ready:
        ready.sort(key=position.__getitem__)
        current = ready.pop(0)
        order.append(nodes[position[current]])
        for nxt in successors[current]:
            waiting[nxt] -= 1
            if waiting[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(nodes):
        raise ValueError("the graph has a dependency cycle")
    return order


def is_test_file(path: str) -> bool:
    """True for a file under a ``tests`` directory or named ``test_*``."""
    parts = PurePosixPath(path).parts
    return "tests" in parts[:-1] or parts[-1].startswith("test_")


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _step(number: int, total: int, node: Node) -> str:
    files = [*node.edit_set.modify, *node.edit_set.create]
    lines = [f"Step {number} of {total}: {node.title} [{node.id}]", node.goal.strip()]
    for label, paths in (
            ("Modify", [p for p in node.edit_set.modify if not is_test_file(p)]),
            ("Create", [p for p in node.edit_set.create if not is_test_file(p)]),
            ("Tests to write", [p for p in files if is_test_file(p)])):
        if paths:
            lines.append(f"{label}: {', '.join(paths)}")
    return "\n".join(lines)


def planned_goal(graph: Graph, order: list[Node]) -> str:
    """The request, the step introduction, and one block per node in ``order``."""
    steps = [_step(number, len(order), node) for number, node in enumerate(order, 1)]
    return "\n\n".join([graph.request.strip(), STEPS_INTRO, *steps])


def _external(symbols: Iterable[str], internal: set[str]) -> list[str]:
    """Symbols that no node of the graph provides or changes, without repeats."""
    return _unique(symbol for symbol in symbols if symbol not in internal)


def flatten_graph(graph: Graph) -> Graph:
    """Return a one-node graph that holds the whole plan of ``graph``.

    The ``planned`` node's goal is the request followed by every node as a
    step (title, goal, files, tests) in topological order. Its edit set,
    ``provides``, check commands, and context files are the unions over all
    nodes; a file created by one node and modified by another is only in
    ``create``, and created files are dropped from ``context_files``.
    ``requires`` and ``requires_impl`` drop every dependency between nodes of
    the graph: they keep only symbols that no node lists in ``provides`` or
    ``edit_set.symbols``, which in a valid graph exist at the base commit.
    Graph-level fields are kept, ``edges`` is empty, and an ``other``
    revision records the flattening. Raises ``ValueError`` for a cyclic graph.
    """
    order = topological_order(graph)
    created = _unique(path for node in order for path in node.edit_set.create)
    created_set = set(created)
    modified = _unique(path for node in order for path in node.edit_set.modify
                       if path not in created_set)
    internal = {symbol for node in order
                for symbol in (*node.provides, *node.edit_set.symbols)}
    requires = _external((s for node in order for s in node.requires), internal)
    requires_impl = [symbol for symbol in _external(
        (s for node in order for s in node.requires_impl), internal)
        if symbol not in requires]
    sizes = [SIZES.index(node.size) for node in order if node.size is not None]
    planned = Node(
        id=PLANNED_ID,
        title=f"Implement the plan ({len(order)} steps)",
        kind="implement",
        goal=planned_goal(graph, order),
        edit_set=EditSet(
            modify=modified, create=created,
            symbols=_unique(s for node in order for s in node.edit_set.symbols)),
        requires=requires,
        requires_impl=requires_impl,
        provides=_unique(s for node in order for s in node.provides),
        check=Check(
            commands=_unique(c for node in order for c in node.check.commands),
            timeout_s=max(node.check.timeout_s for node in order)),
        context_files=_unique(path for node in order for path in node.context_files
                              if path not in created_set),
        size=SIZES[max(sizes)] if sizes else None,
    )
    revision = RevisionEntry(
        action="other", nodes=[node.id for node in order], into=PLANNED_ID,
        reason=(f"flattened from graph {graph.request_id!r} ({len(order)} nodes, "
                f"{len(graph.edges)} edges) into one planned node, steps in "
                "topological order"))
    data = graph.model_dump(mode="json", by_alias=True)
    data.update(nodes=[planned.model_dump(mode="json", by_alias=True)], edges=[],
                revision_log=[*data["revision_log"], revision.model_dump(mode="json")])
    return Graph.model_validate(data)
