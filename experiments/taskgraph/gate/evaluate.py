r"""Evaluate the split gate on four tasks, offline: no model is called.

    python experiments/taskgraph/gate/evaluate.py [--gate-dir C:\tg\gate] [--out <file.md>]

Reads the gate output of each task (``<gate-dir>/<task>.json``, written by
``python -m aqours_code.taskgraph gate --from-report ... --out``), the task's
full plan and its single-agent runs, and prints three tables (also written to
``--out``, default ``<gate-dir>/evaluation.md``):

1. **File hits.** The files the gate says the features modify, against
   (a) the ``modify`` and ``create`` files of every node of the full plan and
   (b) the files each single-agent run changed (``<run>/nodes/solve/diff.patch``).
   Only non-test ``.py`` files count on both sides (``outline.is_source_file``).
   Recall is the share of the reference files the gate found; "recall of
   existing" leaves out files that did not exist at the base commit (the gate
   names only existing files); "extra" counts the gate's files the reference
   does not have.
2. **Decision.** The gate's decision against the method that was cheaper in
   practice (the run costs below, input tokens); dask is close, so either
   decision is accepted and only recorded.
3. **Sensitivity.** The decision again, from the same location, for P in
   0, 2000, 5000 and 10000 and with the material of modified plus reference
   files or of modified files only. The gate's default is P = 5000 with
   reference files; this table does not change it.

A task without a gate output, or whose gate made no decision, is reported as
such and the others are still evaluated.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
AQOURS_ROOT = HERE.parents[2]
sys.path.insert(0, str(AQOURS_ROOT))

from aqours_code.taskgraph.gate import P, decide, features_from_gate  # noqa: E402
from aqours_code.taskgraph.outline import is_source_file  # noqa: E402

DEFAULT_GATE_DIR = Path(r"C:\tg\gate")
P_VALUES = (0, 2000, 5000, 10000)
FILE_MODES = (("modify+reference", True), ("modify only", False))
EITHER = "either"


@dataclass(frozen=True)
class Task:
    """A task: its full plan, its single-agent runs and which method was cheaper."""

    name: str
    plan: Path
    singles: tuple[Path, ...]
    expected: str  # "split", "single" or EITHER
    costs: str


TASKS: tuple[Task, ...] = (
    Task("click", Path(r"C:\tg\solve\click-v3\graph.json"),
         (Path(r"C:\tg\runs\20261012T025828Z-ac6e"), Path(r"C:\tg\runs\20261015T063347Z-879f"),
          Path(r"C:\tg\runs\20261015T223308Z-333c")),
         "split", "v3 about 3.2M in total, single about 4.8M"),
    Task("jp", Path(r"C:\tg\solve\jp-v31\graph.json"),
         (Path(r"C:\tg\runs\20261011T210453Z-93db"),),
         "single", "single 2.56M, v3 6.36M"),
    Task("dvc0.89", Path(r"C:\tg\solve\sweevo-dvc089-v3c\graph.json"),
         (Path(r"C:\tg\runs\20261113T060001Z-3d68"),),
         "split", "v3 5.24M, single 11.62M"),
    Task("dask", Path(r"C:\tg\solve\sweevo-dask0808-v3d\graph.json"),
         (Path(r"C:\tg\runs\20261116T041030Z-e446"),),
         EITHER, "v3 6.68M and 10.61M (two plans), single 10.74M: close"),
)


# ── reading the data ──

def plan_files(graph: dict) -> dict[str, str]:
    """Source files of a plan's nodes: path -> "modify", or "create" when no node modifies it."""
    files: dict[str, str] = {}
    for node in graph.get("nodes") or []:
        edit_set = node.get("edit_set") or {}
        for path in edit_set.get("create") or []:
            if is_source_file(path):
                files.setdefault(path, "create")
        for path in edit_set.get("modify") or []:
            if is_source_file(path):
                files[path] = "modify"
    return files


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def diff_files(patch: str) -> dict[str, str]:
    """Source files a patch changes: path -> "new", "deleted" or "modified"."""
    files: dict[str, str] = {}
    current: str | None = None
    for line in patch.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            current = header.group(2)
            files[current] = "modified"
        elif current is not None and line.startswith("new file mode"):
            files[current] = "new"
        elif current is not None and line.startswith("deleted file mode"):
            files[current] = "deleted"
        elif current is not None and line.startswith("rename to "):
            files[line[len("rename to "):]] = files.pop(current)
            current = line[len("rename to "):]
    return {path: kind for path, kind in files.items() if is_source_file(path)}


def gate_modified(data: dict) -> set[str]:
    """The source files the gate's features modify."""
    return {entry["path"] for feature in data.get("features") or []
            for entry in feature.get("modify") or [] if is_source_file(entry["path"])}


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "not found"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"cannot read: {exc}"
    return (data, None) if isinstance(data, dict) else (None, "not a JSON object")


# ── the three evaluations ──

NEW_KINDS = ("create", "new")


def hits(found: set[str], reference: dict[str, str]) -> dict:
    """How the gate's files ``found`` cover ``reference`` (path -> kind)."""
    existing = {path for path, kind in reference.items() if kind not in NEW_KINDS}
    hit = found & set(reference)
    return {"reference": len(reference), "new": len(reference) - len(existing),
            "gate": len(found), "hits": len(hit),
            "recall": len(hit) / len(reference) if reference else None,
            "recall_existing": len(hit & existing) / len(existing) if existing else None,
            "extra": sorted(found - set(reference)),
            "missed": sorted(set(reference) - found)}


def sensitivity(data: dict) -> dict[tuple[int, str], str]:
    """(P, file mode) -> the decision from the gate's location."""
    features, tokens = features_from_gate(data)
    return {(p, mode): decide(features, tokens, p=p, with_reference=with_reference)["decision"]
            for p in P_VALUES for mode, with_reference in FILE_MODES}


def evaluate_task(task: Task, gate_dir: Path) -> dict:
    """Every number of the three tables for one task."""
    row: dict = {"task": task.name, "expected": task.expected, "costs": task.costs,
                 "gate_path": str(gate_dir / f"{task.name}.json")}
    data, note = _read_json(gate_dir / f"{task.name}.json")
    if data is None:
        row["gate_note"] = f"no gate output ({note})"
        return row
    row["decision"] = data.get("decision")
    if not row["decision"]:
        errors = "; ".join(f"[{issue.get('code')}] {issue.get('message')}"
                           for issue in data.get("errors") or [])
        row["gate_note"] = f"the gate made no decision: {errors or 'no error recorded'}"
        return row
    row.update(reason=data.get("reason"), reason_code=data.get("reason_code"),
               M=(data.get("material") or {}).get("M"),
               share=data.get("largest_group_share"),
               locate_tokens=(data.get("locate") or {}).get("input_tokens"))
    found = gate_modified(data)
    comparisons = []
    graph, graph_note = _read_json(task.plan)
    comparisons.append({"against": "plan", "path": str(task.plan), "note": graph_note,
                        **(hits(found, plan_files(graph)) if graph is not None else {})})
    for run in task.singles:
        patch_path = run / "nodes" / "solve" / "diff.patch"
        try:
            patch = patch_path.read_text(encoding="utf-8", errors="replace")
            result, run_note = hits(found, diff_files(patch)), None
        except OSError as exc:
            result, run_note = {}, f"cannot read {patch_path}: {exc}"
        comparisons.append({"against": f"single {run.name}", "path": str(patch_path),
                            "note": run_note, **result})
    row["hits"] = comparisons
    row["sensitivity"] = {f"P={p} {mode}": decision
                          for (p, mode), decision in sensitivity(data).items()}
    return row


def match(decision: str | None, expected: str) -> str:
    if not decision:
        return "-"
    if expected == EITHER:
        return "either is fine"
    return "yes" if decision == expected else "no"


# ── output ──

def _ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _number(value) -> str:
    return "-" if value is None else f"{value:,}"


def render(rows: list[dict]) -> str:
    """The three tables and the file details as Markdown."""
    lines = ["# Split gate: offline evaluation", "",
             "No model was called: the gate outputs, the plans and the single-agent runs were "
             "read. Non-test `.py` files only.", "",
             "## 1. File hits", "",
             "| task | compared with | reference files (new) | gate files | hits | recall | "
             "recall of existing | extra |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    details = []
    for row in rows:
        if "hits" not in row:
            lines.append(f"| {row['task']} | - | - | - | - | - | - | {row['gate_note']} |")
            continue
        for item in row["hits"]:
            if item.get("note"):
                lines.append(f"| {row['task']} | {item['against']} | - | - | - | - | - | "
                             f"{item['note']} |")
                continue
            lines.append(f"| {row['task']} | {item['against']} | {item['reference']} "
                         f"({item['new']}) | {item['gate']} | {item['hits']} | "
                         f"{_ratio(item['recall'])} | {_ratio(item['recall_existing'])} | "
                         f"{len(item['extra'])} |")
            details.append(f"- {row['task']} vs {item['against']}: missed "
                           f"{', '.join(item['missed']) or 'none'}; extra "
                           f"{', '.join(item['extra']) or 'none'}")
    lines += ["", "Files missed and extra:", "", *details, "",
              "## 2. Decision", "",
              "| task | gate | reason | M | largest group share | cheaper in practice | "
              "expected | match |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        if not row.get("decision"):
            lines.append(f"| {row['task']} | - | {row['gate_note']} | - | - | {row['costs']} | "
                         f"{row['expected']} | - |")
            continue
        lines.append(f"| {row['task']} | {row['decision']} | {row['reason']} | "
                     f"{_number(row['M'])} | {_ratio(row['share'])} | {row['costs']} | "
                     f"{row['expected']} | {match(row['decision'], row['expected'])} |")
    columns = [f"P={p} {mode}" for p in P_VALUES for mode, _ in FILE_MODES]
    default = f"P={P} {FILE_MODES[0][0]}"
    header = [f"{column} (default)" if column == default else column for column in columns]
    lines += ["", "## 3. Sensitivity", "",
              "The decision from the same location; the default rule is unchanged.", "",
              "| task | " + " | ".join(header) + " | expected |",
              "| --- | " + " | ".join("---" for _ in columns) + " | --- |"]
    for row in rows:
        cells = [row.get("sensitivity", {}).get(column, "-") for column in columns]
        lines.append(f"| {row['task']} | " + " | ".join(cells) + f" | {row['expected']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gate-dir", default=str(DEFAULT_GATE_DIR),
                        help=f"directory of <task>.json gate outputs (default {DEFAULT_GATE_DIR})")
    parser.add_argument("--out", help="write the tables here (default <gate-dir>/evaluation.md)")
    parser.add_argument("--json", help="also write every number as JSON to this file")
    args = parser.parse_args(argv)
    gate_dir = Path(args.gate_dir)
    rows = [evaluate_task(task, gate_dir) for task in TASKS]
    text = render(rows)
    print(text, end="")
    out = Path(args.out) if args.out else gate_dir / "evaluation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\nwrote {out}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
