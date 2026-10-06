"""Compare the structure of two task graphs, typically planner versus hand-written."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .repo_index import RepoIndex, build_index
from .schema import Graph, Node
from .validate import edit_files, unique_nodes, usable_edges, validate

TESTS_DIR = "tests/"


def is_test_path(path: str) -> bool:
    """True for files under the top-level ``tests/`` directory."""
    return path.startswith(TESTS_DIR)


def is_test_only(node: Node) -> bool:
    """True when every file the node modifies or creates is under ``tests/``."""
    files = edit_files(node)
    return bool(files) and all(is_test_path(path) for path in files)


def code_files(node: Node) -> set[str]:
    """``modify ∪ create`` without files under ``tests/``."""
    return {path for path in edit_files(node) if not is_test_path(path)}


def jaccard(first: set[str], second: set[str]) -> float:
    """Jaccard similarity; two empty sets are identical."""
    if not first and not second:
        return 1.0
    return len(first & second) / len(first | second)


def depths(graph: Graph) -> dict[str, int] | None:
    """Map node id to the number of nodes on the longest chain ending at it.

    Returns None when the graph has a cycle.
    """
    nodes = [node.id for node in unique_nodes(graph)]
    predecessors: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in usable_edges(graph):
        predecessors[edge.to].add(edge.from_)
    remaining = {node_id: len(preds) for node_id, preds in predecessors.items()}
    successors: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, preds in predecessors.items():
        for pred in preds:
            successors[pred].append(node_id)
    ready = [node_id for node_id in nodes if remaining[node_id] == 0]
    depth = {node_id: 1 for node_id in ready}
    done = 0
    while ready:
        current = ready.pop()
        done += 1
        for nxt in successors[current]:
            depth[nxt] = max(depth.get(nxt, 1), depth[current] + 1)
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                ready.append(nxt)
    return depth if done == len(nodes) else None


def critical_path_length(graph: Graph) -> int | None:
    """Number of nodes on the longest dependency chain; None for a cyclic graph."""
    depth = depths(graph)
    return max(depth.values(), default=0) if depth is not None else None


def max_parallel_width(graph: Graph) -> int | None:
    """Largest layer when nodes are layered by longest-chain depth."""
    depth = depths(graph)
    if depth is None:
        return None
    return max(Counter(depth.values()).values(), default=0)


def graph_stats(graph: Graph, index: RepoIndex | None) -> dict:
    """Structural figures of one graph."""
    report = validate(graph, index)
    nodes = unique_nodes(graph)
    contracts = [node for node in nodes if node.kind == "contract"]
    return {
        "errors": len(report.errors),
        "error_codes": dict(sorted(Counter(report.codes()).items())),
        "warning_codes": dict(sorted(Counter(report.warning_codes()).items())),
        "nodes": len(graph.nodes),
        "contract_nodes": len(contracts),
        "implement_nodes": len(nodes) - len(contracts),
        "contract_files": sorted({path for node in contracts for path in edit_files(node)}),
        "critical_path": critical_path_length(graph),
        "max_parallel_width": max_parallel_width(graph),
        "test_only_nodes": [node.id for node in nodes if is_test_only(node)],
        "edges": len(graph.edges),
    }


def match_nodes(planner: Graph, handwritten: Graph) -> list[dict]:
    """For each hand-written node, the planner node with the most similar files.

    Similarity is the Jaccard index of ``modify ∪ create`` without ``tests/``
    files; ties go to the planner node listed first.
    """
    candidates = unique_nodes(planner)
    matches = []
    for node in unique_nodes(handwritten):
        files = code_files(node)
        best, best_score = None, -1.0
        for other in candidates:
            score = jaccard(files, code_files(other))
            if score > best_score:
                best, best_score = other, score
        matches.append({"handwritten": node.id,
                        "planner": best.id if best is not None else None,
                        "similarity": round(max(best_score, 0.0), 4)})
    return matches


def _index_for(repo: Path, commit: str, cache: dict[str, RepoIndex | None],
               notes: list[str]) -> RepoIndex | None:
    if commit not in cache:
        try:
            cache[commit] = build_index(repo, commit)
        except RuntimeError as exc:
            notes.append(f"no index for {commit}, validated without it: {exc}")
            cache[commit] = None
    return cache[commit]


def compare_graphs(planner: Graph, handwritten: Graph, repo: Path) -> dict:
    """Compare two graphs, each validated against ``repo`` at its own base commit."""
    cache: dict[str, RepoIndex | None] = {}
    notes: list[str] = []
    planner_index = _index_for(repo, planner.base_commit, cache, notes)
    handwritten_index = _index_for(repo, handwritten.base_commit, cache, notes)
    matches = match_nodes(planner, handwritten)
    mean = (sum(match["similarity"] for match in matches) / len(matches)
            if matches else 0.0)
    return {
        "planner": graph_stats(planner, planner_index),
        "handwritten": graph_stats(handwritten, handwritten_index),
        "matches": matches,
        "mean_similarity": round(mean, 4),
        "notes": notes,
    }


def _codes(codes: dict[str, int]) -> str:
    return ", ".join(f"{code}({count})" for code, count in codes.items()) or "none"


def _validation_cell(stats: dict) -> str:
    return f"{stats['errors']} errors; warnings: {_codes(stats['warning_codes'])}"


def _number(value: int | None) -> str:
    return "n/a (cycle)" if value is None else str(value)


def _paths(paths: list[str]) -> str:
    return ", ".join(f"`{path}`" for path in paths) or "none"


def format_comparison(result: dict, planner_name: str = "planner",
                      handwritten_name: str = "handwritten") -> str:
    """Render :func:`compare_graphs` output as a Markdown table."""
    planner, handwritten = result["planner"], result["handwritten"]
    match_lines = [f"{match['handwritten']} -> {match['planner']} ({match['similarity']:.2f})"
                   for match in result["matches"]]
    match_lines.append(f"mean {result['mean_similarity']:.2f}")
    rows = [
        ("Validation", _validation_cell(planner), _validation_cell(handwritten)),
        ("Nodes",
         *(f"{s['nodes']} (contract {s['contract_nodes']}, implement {s['implement_nodes']})"
           for s in (planner, handwritten))),
        ("Contract files", _paths(planner["contract_files"]),
         _paths(handwritten["contract_files"])),
        ("Critical path", _number(planner["critical_path"]),
         _number(handwritten["critical_path"])),
        ("Max parallel width", _number(planner["max_parallel_width"]),
         _number(handwritten["max_parallel_width"])),
        ("Test-only nodes", ", ".join(planner["test_only_nodes"]) or "none",
         ", ".join(handwritten["test_only_nodes"]) or "none"),
        ("Node match (hand-written -> planner)", "<br>".join(match_lines), "-"),
    ]
    lines = [f"| | {planner_name} | {handwritten_name} |", "| --- | --- | --- |"]
    lines += [f"| {label} | {left} | {right} |" for label, left, right in rows]
    lines += [f"\nnote: {note}" for note in result["notes"]]
    return "\n".join(lines) + "\n"
