"""Rule-based edge derivation for missing dependency (V6) and ordering (V5) edges."""
from __future__ import annotations

from typing import Literal

from .repo_index import RepoIndex
from .schema import Edge, Graph, Node, RevisionEntry
from .validate import ancestors, edit_files, unique_nodes


def _try_add_edge(graph: Graph, edge: Edge, entries: list[RevisionEntry],
                  description: str) -> bool:
    """Append ``edge`` unless it would close a cycle; record the outcome."""
    # from -> to closes a cycle exactly when ``to`` can already reach ``from``.
    if edge.from_ == edge.to or edge.to in ancestors(graph)[edge.from_]:
        entries.append(RevisionEntry(
            action="other",
            nodes=[edge.from_, edge.to],
            reason=(f"skipped derived {edge.type} edge {edge.from_} -> {edge.to} "
                    f"({description}): it would create a cycle"),
        ))
        return False
    graph.edges.append(edge)
    entries.append(RevisionEntry(
        action="add_edge",
        nodes=[edge.from_, edge.to],
        reason=f"derived {edge.type} edge {edge.from_} -> {edge.to}: {description}",
    ))
    return True


def _derive_dependency_edges(graph: Graph, index: RepoIndex,
                             entries: list[RevisionEntry]) -> None:
    nodes = unique_nodes(graph)
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        for symbol in node.requires:
            if index.has_symbol(symbol):
                continue
            upstream = ancestors(graph)[node.id]
            if any(symbol in by_id[a].provides for a in upstream):
                continue
            providers = [other for other in nodes
                         if other.id != node.id and symbol in other.provides]
            if len(providers) > 1:
                entries.append(RevisionEntry(
                    action="other",
                    nodes=[node.id, *(other.id for other in providers)],
                    reason=(f"{node.id} requires {symbol}: multiple providers, "
                            "edge not derived"),
                ))
                continue
            if not providers:
                continue
            provider = providers[0]
            edge_type: Literal["interface", "full"] = (
                "interface" if provider.kind == "contract" else "full")
            description = f"{node.id} requires {symbol} provided by {provider.id}"
            _try_add_edge(graph, Edge(
                from_=provider.id, to=node.id, type=edge_type, source="derived",
                reason=description,
            ), entries, description)


def _ordered_pair(first: Node, second: Node) -> tuple[Node, Node]:
    """Contract nodes go first; otherwise keep list order (``first`` precedes)."""
    if second.kind == "contract" and first.kind != "contract":
        return second, first
    return first, second


def _derive_order_edges(graph: Graph, entries: list[RevisionEntry]) -> None:
    nodes = unique_nodes(graph)
    for i, first in enumerate(nodes):
        for second in nodes[i + 1:]:
            overlap = edit_files(first) & edit_files(second)
            if not overlap:
                continue
            ancestor_map = ancestors(graph)
            if (first.id in ancestor_map[second.id]
                    or second.id in ancestor_map[first.id]):
                continue
            upstream, downstream = _ordered_pair(first, second)
            description = (f"{upstream.id} and {downstream.id} both edit "
                           f"{', '.join(sorted(overlap))}")
            _try_add_edge(graph, Edge(
                from_=upstream.id, to=downstream.id, type="order",
                source="derived", reason=description,
            ), entries, description)


def derive_edges(graph: Graph, index: RepoIndex) -> tuple[Graph, list[RevisionEntry]]:
    """Return a copy of ``graph`` with derived edges, plus the revision entries.

    Dependency edges are added first, then ordering edges; ancestry is
    recomputed after every added edge. The entries are also appended to the
    new graph's ``revision_log``. Whether the result is valid is decided by
    :func:`taskgraph.validate.validate`.
    """
    derived = graph.model_copy(deep=True)
    entries: list[RevisionEntry] = []
    _derive_dependency_edges(derived, index, entries)
    _derive_order_edges(derived, entries)
    derived.revision_log.extend(entry.model_copy() for entry in entries)
    return derived, entries
