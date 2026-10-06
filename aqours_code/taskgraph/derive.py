"""Rule-based edge derivation for missing dependency (V6) and ordering (V5) edges."""
from __future__ import annotations

from typing import Literal

from .repo_index import RepoIndex
from .schema import Edge, Graph, Node, RevisionEntry
from .validate import ancestors, edit_files, implementers, unique_nodes


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


_PendingProviders = tuple[str, str, list[str]]


def _derive_dependency_edges(graph: Graph, index: RepoIndex,
                             entries: list[RevisionEntry]) -> list[_PendingProviders]:
    """Add dependency edges (1a requires, 1b requires_impl, 1c context files).

    Return the unresolved ``(node, symbol, providers)`` items from step 1a.
    """
    pending = _derive_interface_edges(graph, index, entries)
    _derive_implementation_edges(graph, entries)
    _derive_context_file_edges(graph, index, entries)
    return pending


def _derive_interface_edges(graph: Graph, index: RepoIndex,
                            entries: list[RevisionEntry]) -> list[_PendingProviders]:
    """Step 1a: one provider per ``requires`` symbol, a contract first."""
    nodes = unique_nodes(graph)
    by_id = {node.id: node for node in nodes}
    pending: list[_PendingProviders] = []
    for node in nodes:
        for symbol in node.requires:
            if index.has_symbol(symbol):
                continue
            upstream = ancestors(graph)[node.id]
            if any(symbol in by_id[a].provides for a in upstream):
                continue
            providers = [other for other in nodes
                         if other.id != node.id and symbol in other.provides]
            contracts = [other for other in providers if other.kind == "contract"]
            # A contract declares the interface, so implement providers of the
            # same symbol do not make it ambiguous.
            candidates = contracts or providers
            if len(candidates) > 1:
                item = (node.id, symbol, [other.id for other in candidates])
                if item not in pending:
                    pending.append(item)
                continue
            if not candidates:
                continue
            provider = candidates[0]
            edge_type: Literal["interface", "full"] = (
                "interface" if provider.kind == "contract" else "full")
            description = f"{node.id} requires {symbol} provided by {provider.id}"
            _try_add_edge(graph, Edge(
                from_=provider.id, to=node.id, type=edge_type, source="derived",
                reason=description,
            ), entries, description)
    return pending


def _derive_implementation_edges(graph: Graph, entries: list[RevisionEntry]) -> None:
    """Step 1b: every implementer of a ``requires_impl`` symbol precedes the node."""
    nodes = unique_nodes(graph)
    for node in nodes:
        for symbol in node.requires_impl:
            for implementer in implementers(nodes, symbol, node.id):
                if implementer.id in ancestors(graph)[node.id]:
                    continue
                description = (f"{node.id} requires implementation of {symbol} "
                               f"by {implementer.id}")
                _try_add_edge(graph, Edge(
                    from_=implementer.id, to=node.id, type="full", source="derived",
                    reason=description,
                ), entries, description)


def _derive_context_file_edges(graph: Graph, index: RepoIndex,
                               entries: list[RevisionEntry]) -> None:
    """Order the single creator of a new context file before its reader."""
    nodes = unique_nodes(graph)
    for node in nodes:
        for ref in node.context_files:
            path = ref.split("#", 1)[0]
            if index.has_file(path):
                continue
            creators = [other for other in nodes if path in other.edit_set.create]
            if len(creators) != 1 or creators[0].id == node.id:
                continue  # no creator (V3), several (V11), or the node itself (V3)
            creator = creators[0]
            if creator.id in ancestors(graph)[node.id]:
                continue
            edge_type: Literal["interface", "full"] = (
                "interface" if creator.kind == "contract" else "full")
            description = (f"{node.id} reads context file {path} created by "
                           f"{creator.id}")
            _try_add_edge(graph, Edge(
                from_=creator.id, to=node.id, type=edge_type, source="derived",
                reason=description,
            ), entries, description)


def _unresolved_provider_entries(graph: Graph,
                                 pending: list[_PendingProviders]) -> list[RevisionEntry]:
    """Record multi-provider symbols still unsatisfied (as V6 sees it) at the end."""
    ancestor_map = ancestors(graph)
    by_id = {node.id: node for node in unique_nodes(graph)}
    return [
        RevisionEntry(
            action="other",
            nodes=[node_id, *providers],
            reason=f"{node_id} requires {symbol}: multiple providers, edge not derived",
        )
        for node_id, symbol, providers in pending
        if not any(symbol in by_id[a].provides for a in ancestor_map[node_id])
    ]


def _creates_what_other_modifies(creator: Node, modifier: Node,
                                 overlap: set[str]) -> list[str]:
    return sorted(path for path in overlap
                  if path in creator.edit_set.create and path in modifier.edit_set.modify)


def _ordered_pair(first: Node, second: Node,
                  overlap: set[str]) -> tuple[Node, Node] | None:
    """Return ``(upstream, downstream)`` for an order edge, or None on conflict.

    Priority: the node that creates a file the other modifies goes first;
    otherwise a contract node goes first; otherwise ``first`` (list order).
    """
    first_creates = _creates_what_other_modifies(first, second, overlap)
    second_creates = _creates_what_other_modifies(second, first, overlap)
    if first_creates and second_creates:
        return None
    if first_creates:
        return first, second
    if second_creates:
        return second, first
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
            pair = _ordered_pair(first, second, overlap)
            if pair is None:
                entries.append(RevisionEntry(
                    action="other",
                    nodes=[first.id, second.id],
                    reason=(
                        f"order edge between {first.id} and {second.id} not derived: "
                        "conflicting creation direction ("
                        f"{first.id} creates "
                        f"{', '.join(_creates_what_other_modifies(first, second, overlap))}"
                        f" modified by {second.id}; {second.id} creates "
                        f"{', '.join(_creates_what_other_modifies(second, first, overlap))}"
                        f" modified by {first.id})"),
                ))
                continue
            upstream, downstream = pair
            description = (f"{upstream.id} and {downstream.id} both edit "
                           f"{', '.join(sorted(overlap))}")
            _try_add_edge(graph, Edge(
                from_=upstream.id, to=downstream.id, type="order",
                source="derived", reason=description,
            ), entries, description)


def derive_edges(graph: Graph, index: RepoIndex) -> tuple[Graph, list[RevisionEntry]]:
    """Return a copy of ``graph`` with derived edges, plus the revision entries.

    Step 1 adds dependency edges:

    1a. ``requires`` (an interface is enough): a single contract provider of
        the symbol gets an ``interface`` edge even if implement nodes also
        provide it; with no contract provider, a single implement provider
        gets a ``full`` edge; several contract providers, or several
        implement providers and no contract, get no edge.
    1b. ``requires_impl``: every implementer (an implement node listing the
        symbol in ``provides`` or ``edit_set.symbols``) that is not yet an
        ancestor gets a ``full`` edge.
    1c. ``context_files`` that a single other node creates (creator -> reader).

    Step 2 adds ordering edges. Ancestry is recomputed after every added
    edge. An ordering edge goes from the node that creates a file the other
    node modifies; otherwise from a contract node; otherwise from the node
    listed first. When each node creates a file the other modifies, no edge
    is added and an ``other`` entry explains the conflict. Entries for added
    edges, for edges skipped because of a cycle, and for creation conflicts
    come first, in the order they happened. For a step 1a symbol left without
    an edge because of several providers, an ``other`` entry is recorded
    after all edges are added, only if no ancestor of the requiring node
    provides the symbol in the final graph. Those entries come last. The
    entries are also appended to the new graph's ``revision_log``.
    Whether the result is valid is decided by
    :func:`aqours_code.taskgraph.validate.validate`.
    """
    derived = graph.model_copy(deep=True)
    entries: list[RevisionEntry] = []
    pending = _derive_dependency_edges(derived, index, entries)
    _derive_order_edges(derived, entries)
    entries.extend(_unresolved_provider_entries(derived, pending))
    derived.revision_log.extend(entry.model_copy() for entry in entries)
    return derived, entries
