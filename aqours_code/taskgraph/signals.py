"""Split signals: two numbers read from a finished plan that hint whether to split.

``python -m aqours_code.taskgraph signals <graph.json>`` reads the graph and the
report the planner wrote next to it (``<graph.json>.report.json``). It calls no
model and writes nothing; the planner and the coordinator do not use these
signals.

A  planning model calls per node: ``totals.model_calls`` of the report divided
   by the number of nodes in the graph (PRE and REG included).
B  the number of node pairs that may run at the same time on one source file:
   neither node is an ancestor of the other along the edges
   :func:`~aqours_code.taskgraph.derive.derive_edges` gives the graph, and the
   two edit sets (``modify``, ``create``, ``shared``) have a ``.py`` file in
   common whose path does not contain ``test``. Common files that are not
   ``.py`` are listed for reference and not counted; common ``.py`` files whose
   path contains ``test`` are neither counted nor listed.

The prediction combines them with :data:`A_SPLIT_MIN`, a provisional threshold.
"""
from __future__ import annotations

import json
from pathlib import Path

from .derive import derive_edges
from .repo_index import RepoIndex, build_index
from .schema import Graph, Node
from .validate import ancestors, unique_nodes

# Provisional: planning calls per node at or above which splitting is predicted
# to pay off. Chosen by hand from three plans (click-v3, jp-v31,
# sweevo-dvc089-v3c); not yet checked against run results.
A_SPLIT_MIN = 1.0

UNKNOWN = "unknown"
SPLIT = "split"
SPLIT_AFTER_SERIALISING = "split-after-serialising"
SINGLE = "single"
UNDETERMINED = "undetermined"


class AssumeAllPresent(RepoIndex):
    """Stand-in index when the repository cannot be read: every file and symbol exists.

    :func:`derive_edges` then derives no edge for a ``requires`` symbol or a
    context file (the edges already in the graph stay); it still derives
    ``requires_impl`` and ordering edges.
    """

    def has_file(self, path: str) -> bool:
        return True

    def has_symbol(self, symbol: str) -> bool:
        return True


def report_path(graph_path: Path) -> Path:
    """Return the path of the planner report written next to ``graph_path``."""
    return graph_path.with_name(graph_path.name + ".report.json")


def load_report(path: Path) -> tuple[dict | None, str | None]:
    """Return ``(report, None)``, or ``(None, why)`` when it cannot be used."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "not found"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read: {exc}"
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(report, dict):
        return None, "not a JSON object"
    return report, None


def signals_index(graph: Graph, repo: str | None) -> tuple[RepoIndex, dict]:
    """Index ``repo`` (default: the graph's ``repo``) at the graph's base commit.

    When that fails, return :class:`AssumeAllPresent` and say why.
    """
    repo = repo or graph.repo
    info: dict = {"repo": repo, "commit": graph.base_commit}
    try:
        index = build_index(Path(repo), graph.base_commit)
    except RuntimeError as exc:
        info.update(built=False, warnings=0,
                    note=(f"{exc}; derive_edges ran with every file and symbol "
                          "treated as present at the base commit"))
        return AssumeAllPresent(commit=graph.base_commit), info
    info.update(built=True, warnings=len(index.warnings), note=None)
    return index, info


def _int_total(totals: dict, key: str) -> int | None:
    value = totals.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def edit_set_files(node: Node) -> set[str]:
    """Return ``modify ∪ create ∪ shared`` of a node."""
    edit_set = node.edit_set
    return set(edit_set.modify) | set(edit_set.create) | set(edit_set.shared)


def counts_for_b(path: str) -> bool:
    """Return whether a common file counts for B: a ``.py`` path without ``test``."""
    return path.endswith(".py") and "test" not in path


def predict(a: float | None, b: int) -> str:
    """Return the provisional prediction for signals A and B."""
    if a is None:
        return UNKNOWN
    if a >= A_SPLIT_MIN:
        return SPLIT if b == 0 else SPLIT_AFTER_SERIALISING
    return SINGLE if b > 0 else UNDETERMINED


def _file_summary(pairs: list[dict], order: dict[str, int]) -> list[dict]:
    """For each file in ``pairs``, the nodes of the pairs that have it in common."""
    nodes_by_file: dict[str, set[str]] = {}
    for pair in pairs:
        for path in pair["files"]:
            nodes_by_file.setdefault(path, set()).update(pair["nodes"])
    summary = [{"path": path, "node_count": len(ids),
                "nodes": sorted(ids, key=order.__getitem__)}
               for path, ids in nodes_by_file.items()]
    summary.sort(key=lambda item: (-item["node_count"], item["path"]))
    return summary


def split_signals(graph: Graph, index: RepoIndex, report: dict | None) -> dict:
    """Compute signals A and B and the provisional prediction for one plan."""
    derived, entries = derive_edges(graph, index)
    ancestor_map = ancestors(derived)
    nodes = unique_nodes(derived)
    order = {node.id: position for position, node in enumerate(nodes)}
    py_pairs: list[dict] = []
    other_pairs: list[dict] = []
    for position, first in enumerate(nodes):
        for second in nodes[position + 1:]:
            if (first.id in ancestor_map[second.id]
                    or second.id in ancestor_map[first.id]):
                continue
            common = edit_set_files(first) & edit_set_files(second)
            py_files = sorted(path for path in common if counts_for_b(path))
            other_files = sorted(path for path in common if not path.endswith(".py"))
            if py_files:
                py_pairs.append({"nodes": [first.id, second.id], "files": py_files})
            if other_files:
                other_pairs.append({"nodes": [first.id, second.id], "files": other_files})

    totals = report.get("totals") if report is not None else None
    calls = _int_total(totals, "model_calls") if isinstance(totals, dict) else None
    tokens = _int_total(totals, "input_tokens") if isinstance(totals, dict) else None
    node_count = len(graph.nodes)
    a = calls / node_count if calls is not None else None
    b = len(py_pairs)
    prediction = predict(a, b)
    serialise: list[str] = []
    if prediction == SPLIT_AFTER_SERIALISING:
        in_pairs = {node_id for pair in py_pairs for node_id in pair["nodes"]}
        serialise = sorted(in_pairs, key=order.__getitem__)
    return {
        "nodes": node_count,
        "edges_in_graph": len(graph.edges),
        "edges_added_by_derive": sum(entry.action == "add_edge" for entry in entries),
        "model_calls": UNKNOWN if calls is None else calls,
        "input_tokens": UNKNOWN if tokens is None else tokens,
        "A": UNKNOWN if a is None else a,
        "B": b,
        "pairs": py_pairs,
        "files": _file_summary(py_pairs, order),
        "non_py_shared": {"pairs": other_pairs,
                          "files": _file_summary(other_pairs, order)},
        "a_split_min": A_SPLIT_MIN,
        "prediction": prediction,
        "serialise": serialise,
        "provisional": True,
    }


def _pairs_lines(pairs: list[dict], files: list[dict]) -> list[str]:
    lines = [f"  {' + '.join(pair['nodes'])}: {', '.join(pair['files'])}"
             for pair in pairs]
    lines.append("  files:")
    lines += [f"    {item['path']}: {item['node_count']} nodes ({', '.join(item['nodes'])})"
              for item in files]
    return lines


def format_signals(result: dict) -> str:
    """Return the signals as readable text."""
    index = result["index"]
    if index["built"]:
        warnings = f", {index['warnings']} index warnings" if index["warnings"] else ""
        index_line = f"index: {index['repo']} at {index['commit'][:12]}{warnings}"
    else:
        index_line = f"index: unavailable ({index['note']})"
    report_line = f"report: {result['report']}"
    if result["report_note"]:
        report_line += f" ({result['report_note']})"
    lines = [
        f"graph: {result['graph']}",
        report_line,
        index_line,
        f"edges: {result['edges_in_graph']} in the graph, "
        f"{result['edges_added_by_derive']} added by derive_edges",
        f"nodes: {result['nodes']}",
    ]
    calls, tokens, a = result["model_calls"], result["input_tokens"], result["A"]
    lines.append(f"planning: {calls} model calls, {tokens} input tokens")
    if a == UNKNOWN:
        lines.append("A = planning calls / nodes = unknown")
    else:
        lines.append(f"A = planning calls / nodes = {calls} / {result['nodes']} = {a:.2f}")
    lines.append(f"B = {result['B']} node pairs that may run in parallel on one "
                 "non-test .py file")
    if result["pairs"]:
        lines += _pairs_lines(result["pairs"], result["files"])
    other = result["non_py_shared"]
    if other["pairs"]:
        lines.append(f"non-.py files in common (reference, not in B): "
                     f"{len(other['pairs'])} pairs")
        lines += _pairs_lines(other["pairs"], other["files"])
    else:
        lines.append("non-.py files in common (reference, not in B): none")
    lines.append(f"prediction (provisional, A_SPLIT_MIN = {result['a_split_min']}): "
                 f"{result['prediction']}")
    if result["serialise"]:
        lines.append(f"serialise: {', '.join(result['serialise'])}")
    return "\n".join(lines) + "\n"
