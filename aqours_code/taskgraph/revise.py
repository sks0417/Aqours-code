"""Step 3 of the planner: revise a graph by rules, without a model.

The planner's Step 2 lists, for each node, the files it edits, and never
merges the items of the draft. ``revise_graph`` takes that graph (edges
already derived) and merges nodes that could only run one after the other on
the same file, where a split costs time and buys nothing:

- **M1**: for an edge X -> Y between two ``implement`` nodes that edit a
  common file, X and Y become one node when the merge delays nobody:

  - every other direct predecessor of Y is an ancestor of X (this also keeps
    the graph acyclic, and X's work does not start later);
  - every other direct successor of X is a descendant of Y, so it had to wait
    for Y anyway (a successor W that only needs X would otherwise have to
    wait for Y's work too).

  When one of the two fails, the nodes stay apart and an ``other`` revision
  says why.
- **M2**: a ``contract`` node whose only direct successor is Y, when every
  other direct predecessor of Y is an ancestor of the contract, is merged
  into Y; the result is an ``implement`` node.

The rules are applied until neither fits, scanning edges and nodes in list
order, so the result is deterministic. Every merge is an entry in
``revision_log``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .schema import Check, Edge, EditSet, Graph, Node, RevisionEntry
from .validate import ancestors, edit_files

CONVENTIONS_HEADING = "Repository conventions:"
EDGE_STRENGTH = {"order": 0, "interface": 1, "full": 2}


def goal_with_conventions(goal: str, conventions: list[str]) -> str:
    """``goal`` followed by a ``Repository conventions:`` section, if there are any."""
    items = [" ".join(item.split()) for item in conventions if item.strip()]
    if not items:
        return goal
    lines = "\n".join(f"- {item}" for item in items)
    return f"{goal.rstrip()}\n\n{CONVENTIONS_HEADING}\n{lines}"


def goal_without_conventions(goal: str, conventions: list[str]) -> str:
    """Undo :func:`goal_with_conventions`."""
    suffix = goal_with_conventions("", conventions)
    if suffix and goal.endswith(suffix):
        return goal[:-len(suffix)].rstrip()
    return goal


@dataclass
class Part:
    """One original node inside a (possibly merged) node."""

    id: str
    title: str
    goal: str


@dataclass
class Merge:
    """One applied merge, as written to the planner report."""

    rule: str
    members: list[str]
    into: str
    files: list[str]

    def to_dict(self) -> dict:
        """JSON form."""
        return {"rule": self.rule, "members": self.members, "into": self.into,
                "files": self.files}


@dataclass
class RevisedGraph:
    """Result of :func:`revise_graph`."""

    graph: Graph
    entries: list[RevisionEntry] = field(default_factory=list)
    merges: list[Merge] = field(default_factory=list)
    not_merged: list[dict] = field(default_factory=list)


def _unique(items) -> list:
    return list(dict.fromkeys(items))


def _predecessors(graph: Graph, node_id: str) -> list[str]:
    return _unique(edge.from_ for edge in graph.edges if edge.to == node_id)


def _successors(graph: Graph, node_id: str) -> list[str]:
    return _unique(edge.to for edge in graph.edges if edge.from_ == node_id)


def _blocking_predecessors(graph: Graph, first: str, second: str) -> list[str]:
    """Direct predecessors of ``second``, other than ``first``, that are not ancestors of ``first``."""
    above = ancestors(graph)[first]
    return [node for node in _predecessors(graph, second)
            if node != first and node not in above]


def _delayed_successors(graph: Graph, first: str, second: str) -> list[str]:
    """Direct successors of ``first``, other than ``second``, that do not wait for ``second``.

    Merging ``first`` and ``second`` would make them wait for ``second``'s work.
    """
    above = ancestors(graph)
    return [node for node in _successors(graph, first)
            if node != second and second not in above[node]]


def _new_id(members: list[str], taken: set[str]) -> str:
    base = "_".join(members)
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}_{number}", number + 1
    return candidate


def merged_goal(parts: list[Part], conventions: list[str]) -> str:
    """The goal of a node made of ``parts``; conventions appear once, at the end."""
    blocks = [f"This sub-task combines {len(parts)} parts. Do them all, in this order."]
    for number, part in enumerate(parts, 1):
        blocks.append(f"Part {number} ({part.id}: {part.title}):\n{part.goal.strip()}")
    return goal_with_conventions("\n\n".join(blocks), conventions)


def merge_nodes(first: Node, second: Node, parts: list[Part], new_id: str,
                conventions: list[str]) -> Node:
    """One ``implement`` node that does ``first`` and then ``second``."""
    created = _unique([*first.edit_set.create, *second.edit_set.create])
    provided = set(first.provides) | set(second.provides)
    requires_impl = _unique(symbol for symbol in (*first.requires_impl, *second.requires_impl)
                            if symbol not in provided)
    requires = _unique(symbol for symbol in (*first.requires, *second.requires)
                       if symbol not in provided and symbol not in requires_impl)
    sizes = [node.size for node in (first, second) if node.size is not None]
    return Node(
        id=new_id,
        title=" + ".join(part.title for part in parts),
        kind="implement",
        goal=merged_goal(parts, conventions),
        edit_set=EditSet(
            any_file=first.edit_set.any_file or second.edit_set.any_file,
            modify=[path for path in _unique([*first.edit_set.modify, *second.edit_set.modify])
                    if path not in created],
            create=created,
            symbols=_unique([*first.edit_set.symbols, *second.edit_set.symbols])),
        requires=requires,
        requires_impl=requires_impl,
        provides=_unique([*first.provides, *second.provides]),
        check=Check(commands=_unique([*first.check.commands, *second.check.commands]),
                    timeout_s=max(first.check.timeout_s, second.check.timeout_s)),
        # A file the merged node creates itself cannot be context for it.
        context_files=[path for path in _unique([*first.context_files, *second.context_files])
                       if path.split("#", 1)[0] not in created],
        size=max(sizes, key=("small", "medium", "large").index) if sizes else None,
    )


def _merged_edges(edges: list[Edge], members: set[str], new_id: str) -> list[Edge]:
    """Redirect edges to the merged node; keep the strongest edge of each pair."""
    best: dict[tuple[str, str], Edge] = {}
    for edge in edges:
        source = new_id if edge.from_ in members else edge.from_
        target = new_id if edge.to in members else edge.to
        if source == target:
            continue
        moved = edge.model_copy(update={"from_": source, "to": target})
        current = best.get((source, target))
        if current is None:
            best[(source, target)] = moved
        elif EDGE_STRENGTH[moved.type] > EDGE_STRENGTH[current.type]:
            # keep the first edge's position, take the stronger edge's content
            best[(source, target)] = moved
    return list(best.values())


def _apply(graph: Graph, first: Node, second: Node, parts: dict[str, list[Part]],
           conventions: list[str]) -> tuple[Graph, str]:
    members = [first.id, second.id]
    taken = {node.id for node in graph.nodes} - set(members)
    combined = parts[first.id] + parts[second.id]
    new_id = _new_id([part.id for part in combined], taken)
    merged = merge_nodes(first, second, combined, new_id, conventions)
    nodes: list[Node] = []
    for node in graph.nodes:
        if node.id in members:
            if not any(existing.id == new_id for existing in nodes):
                nodes.append(merged)
        else:
            nodes.append(node)
    parts[new_id] = combined
    for member in members:
        if member != new_id:
            parts.pop(member, None)
    data = graph.model_dump(mode="json", by_alias=True)
    data["nodes"] = [node.model_dump(mode="json", by_alias=True) for node in nodes]
    data["edges"] = [edge.model_dump(mode="json", by_alias=True)
                     for edge in _merged_edges(graph.edges, set(members), new_id)]
    return Graph.model_validate(data), new_id


def _find_m1(graph: Graph) -> tuple[Node, Node, list[str]] | None:
    by_id = {node.id: node for node in graph.nodes}
    for edge in graph.edges:
        first, second = by_id[edge.from_], by_id[edge.to]
        if first.kind != "implement" or second.kind != "implement":
            continue
        shared = sorted(edit_files(first) & edit_files(second))
        if (shared and not _blocking_predecessors(graph, first.id, second.id)
                and not _delayed_successors(graph, first.id, second.id)):
            return first, second, shared
    return None


def _find_m2(graph: Graph) -> tuple[Node, Node] | None:
    by_id = {node.id: node for node in graph.nodes}
    for node in graph.nodes:
        if node.kind != "contract":
            continue
        downstream = _successors(graph, node.id)
        if len(downstream) == 1 and not _blocking_predecessors(graph, node.id, downstream[0]):
            return node, by_id[downstream[0]]
    return None


def _not_merged(graph: Graph) -> list[dict]:
    """Same-file implement pairs on an edge that M1 had to leave apart."""
    by_id = {node.id: node for node in graph.nodes}
    found, seen = [], set()
    for edge in graph.edges:
        first, second = by_id[edge.from_], by_id[edge.to]
        if (first.kind != "implement" or second.kind != "implement"
                or (first.id, second.id) in seen):
            continue
        shared = sorted(edit_files(first) & edit_files(second))
        blockers = _blocking_predecessors(graph, first.id, second.id)
        delayed = _delayed_successors(graph, first.id, second.id)
        if shared and (blockers or delayed):
            seen.add((first.id, second.id))
            found.append({"rule": "M1", "nodes": [first.id, second.id], "files": shared,
                          "blocked_by": blockers, "delayed": delayed})
    return found


def _why_not_merged(item: dict) -> str:
    first, second = item["nodes"]
    causes = []
    if item["blocked_by"]:
        causes.append(f"{second} also depends on {', '.join(item['blocked_by'])}, which is "
                      f"not an ancestor of {first}")
    if item["delayed"]:
        causes.append(f"{', '.join(item['delayed'])} only needs {first} and would have to "
                      f"wait for {second} too")
    return (f"{first} and {second} both edit {', '.join(item['files'])} but were not merged "
            f"(M1): {'; '.join(causes)}")


def revise_graph(graph: Graph, conventions: list[str] | None = None) -> RevisedGraph:
    """Apply M1 and M2 to ``graph`` until neither fits.

    ``graph`` must be valid and have its edges derived. ``conventions`` are the
    repository conventions already appended to every goal; a merged goal
    carries them once. The returned graph has the merge entries appended to
    its ``revision_log``.
    """
    conventions = list(conventions or [])
    parts = {node.id: [Part(node.id, node.title,
                            goal_without_conventions(node.goal, conventions))]
             for node in graph.nodes}
    result = RevisedGraph(graph=graph)
    while True:
        found = _find_m1(result.graph)
        if found is not None:
            first, second, shared = found
            rule = "M1"
            reason = (f"{first.id} and {second.id} both edit {', '.join(shared)} and can "
                      "only run one after the other (M1)")
        else:
            pair = _find_m2(result.graph)
            if pair is None:
                break
            first, second = pair
            rule, shared = "M2", sorted(edit_files(first) & edit_files(second))
            reason = (f"contract {first.id} has only one downstream node, {second.id}, so it "
                      "is done as part of it (M2)")
        result.graph, new_id = _apply(result.graph, first, second, parts, conventions)
        result.entries.append(RevisionEntry(action="merge", nodes=[first.id, second.id],
                                            into=new_id, reason=reason))
        result.merges.append(Merge(rule, [first.id, second.id], new_id, shared))
    result.not_merged = _not_merged(result.graph)
    for item in result.not_merged:
        first, second = item["nodes"]
        result.entries.append(RevisionEntry(action="other", nodes=[first, second],
                                            reason=_why_not_merged(item)))
    if result.entries:
        result.graph = result.graph.model_copy(
            update={"revision_log": [*result.graph.revision_log, *result.entries]})
    return result
