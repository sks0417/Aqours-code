"""Validation rules V1-V13 and warnings W1-W5 for task graphs."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from .repo_index import RepoIndex
from .schema import Edge, Graph, Node, parse_symbol, split_context_ref


@dataclass
class Issue:
    """One validation error or warning."""

    code: str
    nodes: list[str]
    message: str

    def format(self) -> str:
        """Render as ``[CODE] A, B: message``."""
        if self.nodes:
            return f"[{self.code}] {', '.join(self.nodes)}: {self.message}"
        return f"[{self.code}] {self.message}"


@dataclass
class ValidationReport:
    """Errors and warnings produced by :func:`validate`."""

    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when there are no errors."""
        return not self.errors

    def codes(self) -> list[str]:
        """Return the error codes, in report order."""
        return [issue.code for issue in self.errors]

    def warning_codes(self) -> list[str]:
        """Return the warning codes, in report order."""
        return [issue.code for issue in self.warnings]

    def format(self) -> str:
        """Render the report grouped into errors and warnings."""
        lines = [f"errors ({len(self.errors)}):"]
        lines += [issue.format() for issue in self.errors]
        lines.append(f"warnings ({len(self.warnings)}):")
        lines += [issue.format() for issue in self.warnings]
        return "\n".join(lines)


# ── Graph helpers shared with derive.py ──

def unique_nodes(graph: Graph) -> list[Node]:
    """Return nodes in list order, keeping the first node for a duplicated id."""
    seen: set[str] = set()
    result = []
    for node in graph.nodes:
        if node.id not in seen:
            seen.add(node.id)
            result.append(node)
    return result


def usable_edges(graph: Graph) -> list[Edge]:
    """Edges whose endpoints exist and differ; others are reported by V1."""
    ids = {node.id for node in graph.nodes}
    return [edge for edge in graph.edges
            if edge.from_ in ids and edge.to in ids and edge.from_ != edge.to]


def successors(graph: Graph) -> dict[str, list[str]]:
    """Map node id to its distinct direct successors, in edge order."""
    result: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in usable_edges(graph):
        if edge.to not in result[edge.from_]:
            result[edge.from_].append(edge.to)
    return result


def ancestors(graph: Graph) -> dict[str, set[str]]:
    """Map node id to every node that can reach it along any edge type."""
    predecessors: dict[str, set[str]] = {node.id: set() for node in graph.nodes}
    for edge in usable_edges(graph):
        predecessors[edge.to].add(edge.from_)
    result: dict[str, set[str]] = {}
    for node_id in predecessors:
        seen: set[str] = set()
        queue = deque(predecessors[node_id])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(predecessors[current])
        result[node_id] = seen
    return result


def edit_files(node: Node) -> set[str]:
    """Return ``modify ∪ create`` for a node."""
    return set(node.edit_set.modify) | set(node.edit_set.create)


def _changes_symbol(node: Node, symbol: str) -> bool:
    return symbol in node.provides or symbol in node.edit_set.symbols


def implementers(nodes: list[Node], symbol: str, exclude: str) -> list[Node]:
    """Implement nodes that list ``symbol`` in provides or edit_set.symbols."""
    return [node for node in nodes
            if node.id != exclude and node.kind == "implement"
            and _changes_symbol(node, symbol)]


def declarers(nodes: list[Node], symbol: str, exclude: str) -> list[Node]:
    """Contract nodes that list ``symbol`` in provides."""
    return [node for node in nodes
            if node.id != exclude and node.kind == "contract"
            and symbol in node.provides]


def modifiers(nodes: list[Node], symbol: str, exclude: str) -> list[Node]:
    """Nodes of any kind that list ``symbol`` in provides or edit_set.symbols."""
    return [node for node in nodes
            if node.id != exclude and _changes_symbol(node, symbol)]


def _find_cycles(graph: Graph) -> list[list[str]]:
    """Return one concrete cycle per strongly connected component (size > 1)."""
    order = {node.id: index for index, node in enumerate(unique_nodes(graph))}
    adjacency = successors(graph)
    ancestor_map = ancestors(graph)
    cycles = []
    assigned: set[str] = set()
    for node_id in order:
        if node_id in assigned or node_id not in ancestor_map[node_id]:
            continue
        component = {node_id} | {
            other for other in ancestor_map[node_id]
            if node_id in ancestor_map[other]
        }
        assigned |= component
        start = min(component, key=order.__getitem__)
        cycles.append(_cycle_path(start, component, adjacency))
    return cycles


def _cycle_path(start: str, component: set[str],
                adjacency: dict[str, list[str]]) -> list[str]:
    """Breadth-first search for the shortest path from ``start`` back to itself."""
    parents: dict[str, str] = {}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in adjacency[current]:
            if nxt not in component:
                continue
            if nxt == start:
                path = [current]
                while path[-1] != start:
                    path.append(parents[path[-1]])
                return list(reversed(path))
            if nxt not in parents:
                parents[nxt] = current
                queue.append(nxt)
    return [start]


# ── Rules ──

def _check_structure(graph: Graph, report: ValidationReport) -> None:
    counts = Counter(node.id for node in graph.nodes)
    for node_id, count in counts.items():
        if count > 1:
            report.errors.append(Issue(
                "V1", [node_id], f"node id is used by {count} nodes"))
    ids = set(counts)
    seen_edges: set[tuple[str, str, str]] = set()
    for edge in graph.edges:
        missing = [name for name in (edge.from_, edge.to) if name not in ids]
        if missing:
            report.errors.append(Issue(
                "V1", [edge.from_, edge.to],
                f"edge references unknown node(s): {', '.join(dict.fromkeys(missing))}"))
            continue
        if edge.from_ == edge.to:
            report.errors.append(Issue(
                "V1", [edge.from_], f"self-loop {edge.type} edge"))
            continue
        key = (edge.from_, edge.to, edge.type)
        if key in seen_edges:
            report.errors.append(Issue(
                "V1", [edge.from_, edge.to],
                f"duplicate {edge.type} edge {edge.from_} -> {edge.to}"))
        seen_edges.add(key)


def _check_acyclic(graph: Graph, report: ValidationReport) -> None:
    for cycle in _find_cycles(graph):
        path = " -> ".join([*cycle, cycle[0]])
        report.errors.append(Issue("V2", cycle, f"dependency cycle: {path}"))


def _check_files(graph: Graph, index: RepoIndex, ancestor_map: dict[str, set[str]],
                 report: ValidationReport) -> None:
    created = {path for node in graph.nodes for path in node.edit_set.create}
    for node in graph.nodes:
        for path in node.edit_set.modify:
            if index.has_file(path):
                continue
            creators = [other.id for other in unique_nodes(graph)
                        if other.id != node.id and path in other.edit_set.create]
            if any(creator in ancestor_map[node.id] for creator in creators):
                continue
            if creators:
                names = ", ".join(creators)
                verb = "is" if len(creators) == 1 else "are"
                report.errors.append(Issue(
                    "V3", [node.id, *creators],
                    f"modifies {path}, which is created by {names}, but {names} "
                    f"{verb} not an ancestor of {node.id} (missing edge?)"))
            else:
                report.errors.append(Issue(
                    "V3", [node.id],
                    f"modifies {path}, which does not exist at the base commit"))
        for path in node.edit_set.create:
            if index.has_file(path):
                report.errors.append(Issue(
                    "V3", [node.id],
                    f"creates {path}, which already exists at the base commit"))
        for ref in node.context_files:
            path, title = split_context_ref(ref)
            if title is not None and title not in index.markdown_titles.get(path, []):
                report.errors.append(Issue(
                    "V3", [node.id], f"context heading {ref} does not exist at the base commit"))
            if index.has_file(path):
                continue
            if path not in created:
                report.errors.append(Issue(
                    "V3", [node.id],
                    f"context file {path} neither exists at the base commit "
                    "nor is created by any node"))
                continue
            # A node never reads its own new file: it does not exist yet when
            # the node starts, so the node itself counts as a non-ancestor.
            creators = [other.id for other in unique_nodes(graph)
                        if path in other.edit_set.create]
            if any(creator in ancestor_map[node.id] for creator in creators):
                continue
            if creators == [node.id]:
                report.errors.append(Issue(
                    "V3", [node.id],
                    f"context file {path} is created by {node.id} itself, so it "
                    f"does not exist when {node.id} starts"))
                continue
            names = ", ".join(creators)
            which = ("which is not an ancestor" if len(creators) == 1
                     else "which are not ancestors")
            report.errors.append(Issue(
                "V3", [node.id, *(c for c in creators if c != node.id)],
                f"context file {path} is created by {names}, {which} of "
                f"{node.id} (missing edge?)"))


def _check_commands(graph: Graph, report: ValidationReport) -> None:
    for node in graph.nodes:
        if not any(command.strip() for command in node.check.commands):
            report.errors.append(Issue(
                "V4", [node.id], "check.commands has no non-empty command"))


def _check_edit_conflicts(graph: Graph, ancestor_map: dict[str, set[str]],
                          report: ValidationReport) -> None:
    nodes = unique_nodes(graph)
    for i, first in enumerate(nodes):
        for second in nodes[i + 1:]:
            overlap = edit_files(first) & edit_files(second)
            if not overlap:
                continue
            if (first.id in ancestor_map[second.id]
                    or second.id in ancestor_map[first.id]):
                continue
            report.errors.append(Issue(
                "V5", [first.id, second.id],
                f"both edit {', '.join(sorted(overlap))} but neither is an "
                "ancestor of the other"))


def _check_requires(graph: Graph, index: RepoIndex,
                    ancestor_map: dict[str, set[str]],
                    report: ValidationReport) -> None:
    nodes = unique_nodes(graph)
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        for symbol in node.requires:
            if index.has_symbol(symbol):
                continue
            if any(symbol in by_id[a].provides for a in ancestor_map[node.id]):
                continue
            providers = [other.id for other in nodes
                         if other.id != node.id and symbol in other.provides]
            if providers:
                report.errors.append(Issue(
                    "V6", [node.id, *providers],
                    f"requires {symbol}, provided by {', '.join(providers)} which "
                    f"is not an ancestor of {node.id} (missing dependency edge?)"))
            else:
                report.errors.append(Issue(
                    "V6", [node.id],
                    f"requires {symbol}, which is neither defined at the base "
                    "commit nor provided by another node"))


def _check_edit_set_nonempty(graph: Graph, report: ValidationReport) -> None:
    for node in graph.nodes:
        if node.edit_set.any_file and len(graph.nodes) != 1:
            report.errors.append(Issue(
                "V13", [node.id], "edit_set.any_file is allowed only in a single-node graph"))
        if not node.edit_set.any_file and not edit_files(node):
            report.errors.append(Issue(
                "V7", [node.id], "edit_set.modify and edit_set.create are both empty"))


def _check_symbol_format(graph: Graph, report: ValidationReport) -> None:
    for node in graph.nodes:
        for field_name, values in (("requires", node.requires),
                                   ("requires_impl", node.requires_impl),
                                   ("provides", node.provides),
                                   ("edit_set.symbols", node.edit_set.symbols)):
            for value in values:
                try:
                    parse_symbol(value)
                except ValueError as exc:
                    report.errors.append(Issue(
                        "V8", [node.id], f"invalid symbol in {field_name}: {exc}"))


def _check_interface_sources(graph: Graph, report: ValidationReport) -> None:
    by_id = {node.id: node for node in unique_nodes(graph)}
    for edge in usable_edges(graph):
        upstream = by_id[edge.from_]
        if edge.type == "interface" and upstream.kind != "contract":
            report.errors.append(Issue(
                "V9", [edge.from_, edge.to],
                f"interface edge {edge.from_} -> {edge.to} starts at a "
                f"{upstream.kind} node; interface edges must start at a "
                "contract node"))


def _check_symbol_files(graph: Graph, report: ValidationReport) -> None:
    for node in graph.nodes:
        if node.edit_set.any_file:
            continue
        files = edit_files(node)
        for label, symbols in (("edit_set.symbols lists", node.edit_set.symbols),
                               ("provides", node.provides)):
            for symbol in symbols:
                try:
                    path = parse_symbol(symbol).path
                except ValueError:
                    continue  # reported by V8
                if path not in files:
                    report.errors.append(Issue(
                        "V10", [node.id],
                        f"{label} {symbol}, but {path} is not in "
                        "edit_set.modify or edit_set.create"))


def _warn_changed_existing_symbols(graph: Graph, index: RepoIndex,
                                   ancestor_map: dict[str, set[str]],
                                   report: ValidationReport) -> None:
    nodes = unique_nodes(graph)
    for node in nodes:
        for symbol in node.requires:
            if not index.has_symbol(symbol):
                continue
            changers = [other.id for other in modifiers(nodes, symbol, node.id)
                        if other.id not in ancestor_map[node.id]]
            if changers:
                which = ("which is not an ancestor" if len(changers) == 1
                         else "which are not ancestors")
                report.warnings.append(Issue(
                    "W5", [node.id, *changers],
                    f"requires {symbol}; {symbol} exists at the base commit but "
                    f"is changed by {', '.join(changers)} {which} of {node.id}"))


def _check_required_implementations(graph: Graph, index: RepoIndex,
                                    ancestor_map: dict[str, set[str]],
                                    report: ValidationReport) -> None:
    nodes = unique_nodes(graph)
    for node in nodes:
        for symbol in node.requires_impl:
            imps = implementers(nodes, symbol, node.id)
            if imps:
                missing = [imp.id for imp in imps
                           if imp.id not in ancestor_map[node.id]]
                if missing:
                    which = ("is not an ancestor" if len(missing) == 1
                             else "are not ancestors")
                    report.errors.append(Issue(
                        "V12", [node.id, *missing],
                        f"requires the implementation of {symbol}, but "
                        f"{', '.join(missing)} {which} of {node.id} (missing edge?)"))
                continue
            if index.has_symbol(symbol):
                continue  # the existing implementation is used
            contracts = [contract.id for contract in declarers(nodes, symbol, node.id)]
            if contracts:
                report.errors.append(Issue(
                    "V12", [node.id, *contracts],
                    f"requires the implementation of {symbol}, but {symbol} is "
                    f"only declared by contract {', '.join(contracts)}; no "
                    "implement node implements it"))
            else:
                report.errors.append(Issue(
                    "V12", [node.id],
                    f"requires the implementation of {symbol}, which is neither "
                    "defined at the base commit nor implemented by any node"))


def _check_single_creator(graph: Graph, report: ValidationReport) -> None:
    creators: dict[str, list[str]] = {}
    for node in unique_nodes(graph):
        for path in node.edit_set.create:
            creators.setdefault(path, []).append(node.id)
    for path, node_ids in creators.items():
        if len(node_ids) > 1:
            report.errors.append(Issue(
                "V11", node_ids,
                f"{path} is created by {len(node_ids)} nodes; each new file "
                "must have exactly one creator"))


def _warn_order_edge_for_required_symbols(graph: Graph,
                                          report: ValidationReport) -> None:
    edge_types: dict[tuple[str, str], set[str]] = {}
    for edge in usable_edges(graph):
        edge_types.setdefault((edge.from_, edge.to), set()).add(edge.type)
    by_id = {node.id: node for node in unique_nodes(graph)}
    for (upstream_id, downstream_id), types in edge_types.items():
        if types != {"order"}:
            continue
        upstream, downstream = by_id[upstream_id], by_id[downstream_id]
        shared = [symbol for symbol in downstream.requires
                  if symbol in upstream.provides]
        shared_impl = [symbol for symbol in downstream.requires_impl
                       if upstream.kind == "implement"
                       and _changes_symbol(upstream, symbol)]
        needs = []
        if shared:
            needs.append(f"requires {', '.join(shared)}")
        if shared_impl:
            needs.append(f"requires the implementation of {', '.join(shared_impl)}")
        if needs:
            report.warnings.append(Issue(
                "W4", [upstream_id, downstream_id],
                f"{downstream_id} {' and '.join(needs)} from {upstream_id}, but "
                f"{upstream_id} -> {downstream_id} is only an order edge; use "
                "interface or full"))


def _warn_small_single_successor(graph: Graph, report: ValidationReport) -> None:
    adjacency = successors(graph)
    for node in unique_nodes(graph):
        downstream = adjacency[node.id]
        if node.size == "small" and len(downstream) == 1:
            report.warnings.append(Issue(
                "W1", [node.id, downstream[0]],
                f"small node with a single downstream node; consider merging "
                f"it into {downstream[0]}"))


def _warn_unused_provides(graph: Graph, report: ValidationReport) -> None:
    for node in graph.nodes:
        for symbol in node.provides:
            if not any(symbol in other.requires or symbol in other.requires_impl
                       for other in graph.nodes
                       if other is not node and other.id != node.id):
                report.warnings.append(Issue(
                    "W2", [node.id],
                    f"provides {symbol}, which no other node requires"))


def validate(graph: Graph, index: RepoIndex | None = None) -> ValidationReport:
    """Check ``graph`` against rules V1-V13 and warnings W1-W5.

    Checks that need repository information (V3, V6, V12, W5) are skipped,
    with a warning, when ``index`` is None.
    """
    report = ValidationReport()
    ancestor_map = ancestors(graph)
    _check_structure(graph, report)
    _check_acyclic(graph, report)
    if index is not None:
        _check_files(graph, index, ancestor_map, report)
    _check_commands(graph, report)
    _check_edit_conflicts(graph, ancestor_map, report)
    if index is not None:
        _check_requires(graph, index, ancestor_map, report)
    _check_edit_set_nonempty(graph, report)
    _check_symbol_format(graph, report)
    _check_interface_sources(graph, report)
    _check_symbol_files(graph, report)
    _check_single_creator(graph, report)
    if index is not None:
        _check_required_implementations(graph, index, ancestor_map, report)

    if index is None:
        for code in ("V3", "V6", "V12", "W5"):
            report.warnings.append(Issue(
                code, [], "skipped: no repository index was provided"))
    _warn_small_single_successor(graph, report)
    _warn_unused_provides(graph, report)
    if not any(command.strip() for command in graph.final_checks):
        report.warnings.append(Issue("W3", [], "final_checks is empty"))
    _warn_order_edge_for_required_symbols(graph, report)
    if index is not None:
        _warn_changed_existing_symbols(graph, index, ancestor_map, report)
    return report
