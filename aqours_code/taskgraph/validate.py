"""Validation rules V1-V10 and warnings W1-W4 for task graphs."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from .repo_index import RepoIndex
from .schema import Edge, Graph, Node, parse_symbol


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


def _check_files(graph: Graph, index: RepoIndex, report: ValidationReport) -> None:
    created = {path for node in graph.nodes for path in node.edit_set.create}
    for node in graph.nodes:
        for path in node.edit_set.modify:
            if not index.has_file(path):
                report.errors.append(Issue(
                    "V3", [node.id],
                    f"modifies {path}, which does not exist at the base commit"))
        for path in node.edit_set.create:
            if index.has_file(path):
                report.errors.append(Issue(
                    "V3", [node.id],
                    f"creates {path}, which already exists at the base commit"))
        for path in node.context_files:
            if not index.has_file(path) and path not in created:
                report.errors.append(Issue(
                    "V3", [node.id],
                    f"context file {path} neither exists at the base commit "
                    "nor is created by any node"))


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
        if not edit_files(node):
            report.errors.append(Issue(
                "V7", [node.id], "edit_set.modify and edit_set.create are both empty"))


def _check_symbol_format(graph: Graph, report: ValidationReport) -> None:
    for node in graph.nodes:
        for field_name, values in (("requires", node.requires),
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
        files = edit_files(node)
        for symbol in node.edit_set.symbols:
            try:
                path = parse_symbol(symbol).path
            except ValueError:
                continue  # reported by V8
            if path not in files:
                report.errors.append(Issue(
                    "V10", [node.id],
                    f"edit_set.symbols lists {symbol}, but {path} is not in "
                    "edit_set.modify or edit_set.create"))


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
        if shared:
            report.warnings.append(Issue(
                "W4", [upstream_id, downstream_id],
                f"{downstream_id} requires {', '.join(shared)} from "
                f"{upstream_id}, but {upstream_id} -> {downstream_id} is only an "
                "order edge; use interface or full"))


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
            if not any(symbol in other.requires for other in graph.nodes
                       if other is not node and other.id != node.id):
                report.warnings.append(Issue(
                    "W2", [node.id],
                    f"provides {symbol}, which no other node requires"))


def validate(graph: Graph, index: RepoIndex | None = None) -> ValidationReport:
    """Check ``graph`` against rules V1-V10 and warnings W1-W4.

    Rules that need repository information (V3, V6) are skipped, with a
    warning, when ``index`` is None.
    """
    report = ValidationReport()
    ancestor_map = ancestors(graph)
    _check_structure(graph, report)
    _check_acyclic(graph, report)
    if index is not None:
        _check_files(graph, index, report)
    _check_commands(graph, report)
    _check_edit_conflicts(graph, ancestor_map, report)
    if index is not None:
        _check_requires(graph, index, ancestor_map, report)
    _check_edit_set_nonempty(graph, report)
    _check_symbol_format(graph, report)
    _check_interface_sources(graph, report)
    _check_symbol_files(graph, report)

    if index is None:
        for code in ("V3", "V6"):
            report.warnings.append(Issue(
                code, [], "skipped: no repository index was provided"))
    _warn_small_single_successor(graph, report)
    _warn_unused_provides(graph, report)
    if not any(command.strip() for command in graph.final_checks):
        report.warnings.append(Issue("W3", [], "final_checks is empty"))
    _warn_order_edge_for_required_symbols(graph, report)
    return report
