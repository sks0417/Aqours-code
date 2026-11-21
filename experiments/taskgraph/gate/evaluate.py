r"""Evaluate the split gate on four tasks, offline: no model is called.

    python experiments/taskgraph/gate/evaluate.py [--gate-dir C:\tg\gate] [--out <file.md>]
        [--append] [--json <file>]
    python experiments/taskgraph/gate/evaluate.py --ideal [--out <file.md>] [--append]
        [--json <file>]

Two modes; each prints its tables and writes them to ``--out`` (default
``<gate-dir>/evaluation.md``; ``--append`` adds them after what is there).

- **Gate outputs** (default): the location the gate found, read from
  ``<gate-dir>/<task>.json`` (``python -m aqours_code.taskgraph gate
  --from-report ... --out``). First the file hits: the files the gate says
  the features modify, against (a) the ``modify`` and ``create`` files of
  every node of the full plan and (b) the files each single-agent run changed
  (``<run>/nodes/solve/diff.patch``). Recall is the share of the reference
  files the gate found; "recall of existing" leaves out files that did not
  exist at the base commit (the gate names only existing files); "extra"
  counts the gate's files the reference does not have. Then the decision,
  made again from the gate's location with the W it recorded.
- **Ideal location** (``--ideal``): no gate output; the location is the full
  plan's. Every node except PRE and REG is a feature: its ``modify`` source
  files are the files it modifies, and its first three ``context_files``
  that are source files (other than its own) are its references. File sizes
  are read from the plan's repository at its base commit. W comes from the
  compaction trigger (``gate.agent_window``).

Only source files count (``.py`` files that are not tests). The decision is
given for two kinds of material: modified plus reference files (the gate's
rule) and modified files only. It is compared with the method that was
cheaper in practice (the run costs below, input tokens): a decision in the
``unclear`` zone is the default and counted neither right nor wrong; dask is
close, so either decision is accepted there.

A task whose data is missing is reported as such and the others are still
evaluated.
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

from aqours_code.taskgraph import gitops  # noqa: E402
from aqours_code.taskgraph.gate import (MAX_REFERENCE, UNCLEAR, FeatureFiles,  # noqa: E402
                                        Window, agent_window, decide, estimate_tokens,
                                        features_from_gate, half)
from aqours_code.taskgraph.outline import is_source_file  # noqa: E402

DEFAULT_GATE_DIR = Path(r"C:\tg\gate")
MODES = (("modify+reference", True), ("modify only", False))
EITHER = "either"
DEFAULT = "default"
PLAN_ONLY_NODES = ("PRE", "REG")


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

def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "not found"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"cannot read: {exc}"
    return (data, None) if isinstance(data, dict) else (None, "not a JSON object")


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


def ideal_features(graph: dict, report: dict | None = None) -> list[FeatureFiles]:
    """The full plan's location: every node but PRE and REG, as a feature.

    Its modified source files, and as references its first ``MAX_REFERENCE``
    context files that are source files and not among its own (a context
    file's ``#section`` is left out).
    """
    excluded = set(PLAN_ONLY_NODES)
    if report is not None:
        excluded |= {report.get(key) for key in ("pre_node", "reg_node")} - {None}
    features = []
    for node in graph.get("nodes") or []:
        if node.get("id") in excluded:
            continue
        edit_set = node.get("edit_set") or {}
        modify = list(dict.fromkeys(path for path in edit_set.get("modify") or []
                                    if is_source_file(path)))
        references: list[str] = []
        for entry in node.get("context_files") or []:
            path = str(entry).split("#", 1)[0]
            if is_source_file(path) and path not in modify and path not in references:
                references.append(path)
        features.append(FeatureFiles(str(node["id"]), str(node.get("title", "")), modify,
                                     references[:MAX_REFERENCE]))
    return features


def tokens_at(repo: Path, commit: str, paths) -> tuple[dict[str, int], list[str]]:
    """Estimated tokens of each file at ``commit``; files not there count 0 and are listed."""
    tokens: dict[str, int] = {}
    missing: list[str] = []
    for path in dict.fromkeys(paths):
        blob = gitops.blob_at(repo, commit, path)
        if blob is None:
            missing.append(path)
            tokens[path] = 0
        else:
            tokens[path] = estimate_tokens(len(blob.decode("utf-8", errors="replace")))
    return tokens, missing


# ── the evaluations ──

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


def outcome(decision: str, zone: str, expected: str) -> str:
    """right / wrong; "default" in the unclear zone; "either is fine" when both are."""
    if zone == UNCLEAR:
        return DEFAULT
    if expected == EITHER:
        return "either is fine"
    return "right" if decision == expected else "wrong"


def decisions(features: list[FeatureFiles], tokens: dict[str, int], w: int,
              expected: str) -> dict:
    """The decision for both kinds of material, and how each compares with ``expected``."""
    modes = {}
    for mode, with_reference in MODES:
        result = decide(features, tokens, w=w, with_reference=with_reference)
        modes[mode] = {"decision": result["decision"], "zone": result["zone"],
                       "reason": result["reason"], "M": result["material"]["M"],
                       "outcome": outcome(result["decision"], result["zone"], expected),
                       "oversized": result["oversized"]}
        if with_reference:
            groups, concentration = result["groups"], result["concentration"]
            shared = result["shared"]
    return {"modes": modes, "features": len(features), "groups": groups,
            "concentration": concentration, "shared": shared, "W": w, "half_W": half(w)}


def _base_row(task: Task) -> dict:
    return {"task": task.name, "expected": task.expected, "costs": task.costs}


def evaluate_ideal_task(task: Task, window: Window) -> dict:
    """The decision from the full plan's location, sizes read at its base commit."""
    row = _base_row(task)
    graph, note = _read_json(task.plan)
    if graph is None:
        row["note"] = f"no plan ({note}): {task.plan}"
        return row
    report, _ = _read_json(task.plan.with_name(task.plan.name + ".report.json"))
    features = ideal_features(graph, report)
    paths = [path for feature in features for path in feature.modify + feature.reference]
    try:
        tokens, missing = tokens_at(Path(graph["repo"]), graph["base_commit"], paths)
    except (KeyError, gitops.GitError, OSError) as exc:
        row["note"] = f"cannot read the plan's repository: {exc}"
        return row
    row.update(decisions(features, tokens, window.tokens, task.expected))
    row["missing_at_base"] = missing
    row["location"] = [{"id": feature.id, "modify": feature.modify,
                        "reference": feature.reference} for feature in features]
    return row


def evaluate_gate_task(task: Task, gate_dir: Path) -> dict:
    """File hits and the decision from the gate's location."""
    row = _base_row(task)
    data, note = _read_json(gate_dir / f"{task.name}.json")
    if data is None:
        row["note"] = f"no gate output ({note})"
        return row
    if not data.get("decision"):
        errors = "; ".join(f"[{issue.get('code')}] {issue.get('message')}"
                           for issue in data.get("errors") or [])
        row["note"] = f"the gate made no decision: {errors or 'no error recorded'}"
        return row
    w = ((data.get("window") or {}).get("W") or (data.get("material") or {}).get("W")
         or agent_window().tokens)
    features, tokens = features_from_gate(data)
    row.update(decisions(features, tokens, w, task.expected))
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
    return row


# ── output ──

def _ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _hits_table(rows: list[dict]) -> list[str]:
    lines = ["## File hits", "",
             "| task | compared with | reference files (new) | gate files | hits | recall | "
             "recall of existing | extra |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    details = []
    for row in rows:
        if "hits" not in row:
            lines.append(f"| {row['task']} | - | - | - | - | - | - | {row['note']} |")
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
    return lines + ["", "Files missed and extra:", "", *(details or ["- (none)"]), ""]


def _cell(mode: dict) -> str:
    return f"{mode['decision']} ({mode['zone']})"


def _oversized(entries: list[dict]) -> str:
    return ", ".join(f"{entry['path']} ({entry['tokens']:,})" for entry in entries) or "-"


def _counts(rows: list[dict], mode: str) -> str:
    counted = [row["modes"][mode]["outcome"] for row in rows if "modes" in row]
    parts = [f"{name} {counted.count(name)}"
             for name in ("right", "wrong", DEFAULT, "either is fine")]
    return f"{mode}: " + ", ".join(parts) + f" ({DEFAULT}: not counted)"


def _decision_tables(rows: list[dict]) -> list[str]:
    names = [mode for mode, _ in MODES]
    lines = ["## Decision", "",
             "| task | cheaper in practice | expected | " +
             " | ".join(f"{name} | result" for name in names) + " |",
             "| --- | --- | --- | " + " | ".join("--- | ---" for _ in names) + " |"]
    for row in rows:
        if "modes" not in row:
            cells = " | ".join("- | -" for _ in names)
            lines.append(f"| {row['task']} | {row['costs']} | {row['expected']} | {cells} |")
            continue
        cells = " | ".join(f"{_cell(row['modes'][name])} | {row['modes'][name]['outcome']}"
                           for name in names)
        lines.append(f"| {row['task']} | {row['costs']} | {row['expected']} | {cells} |")
    lines += ["", *[f"- {_counts(rows, name)}" for name in names], "",
              "## Numbers", "",
              "| task | features | largest group | M (" + names[0] + ") | M (" + names[1] +
              ") | W/2 | W | over W |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    notes, reasons, groups = [], [], []
    for row in rows:
        if "modes" not in row:
            lines.append(f"| {row['task']} | - | - | - | - | - | - | - |")
            notes.append(f"- {row['task']}: {row['note']}")
            continue
        modes, concentration = row["modes"], row["concentration"]
        lines.append(f"| {row['task']} | {row['features']} | {concentration['of']} = "
                     f"{concentration['value']:.2f} | {modes[names[0]]['M']:,} | "
                     f"{modes[names[1]]['M']:,} | {row['half_W']:,} | {row['W']:,} | "
                     f"{_oversized(modes[names[0]]['oversized'])} |")
        reasons += [f"- {row['task']}, {name}: {modes[name]['reason']}" for name in names]
        shared = [group for group in row["groups"] if group["shared_files"]]
        groups += [f"- {row['task']}: {', '.join(group['features'])}: shared "
                   f"{', '.join(group['shared_files'])}" for group in shared]
        if not shared:
            groups.append(f"- {row['task']}: no two features share a modified file")
        if row.get("missing_at_base"):
            notes.append(f"- {row['task']}: not at the base commit (0 tokens): "
                         f"{', '.join(row['missing_at_base'])}")
    lines += ["", "Reasons:", "", *reasons, "", "Groups with shared modified files:", "",
              *groups, ""]
    if notes:
        lines += ["Notes:", "", *notes, ""]
    return lines


def render(rows: list[dict], ideal: bool, window: Window | None = None) -> str:
    """The tables of one mode as Markdown."""
    if ideal:
        lines = ["# Split gate evaluation: ideal location", "",
                 "No model was called. The location is the full plan's (every node but PRE "
                 "and REG; its modified source files and its first three source context "
                 "files); file sizes are read at the plan's base commit."]
        if window is not None:
            lines += ["", f"W = {window.tokens:,} (compaction trigger {window.trigger:,} from "
                          f"the {window.source}); W/2 = {half(window.tokens):,}."]
        lines.append("")
    else:
        lines = ["# Split gate evaluation: gate outputs", "",
                 "No model was called: the gate outputs, the plans and the single-agent runs "
                 "were read. The decision is made again from each gate's location with the "
                 "W it recorded. Non-test `.py` files only.", "",
                 *_hits_table(rows)]
    lines += _decision_tables(rows)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ideal", action="store_true",
                        help="use the full plans' location instead of the gate outputs")
    parser.add_argument("--gate-dir", default=str(DEFAULT_GATE_DIR),
                        help=f"directory of <task>.json gate outputs (default {DEFAULT_GATE_DIR})")
    parser.add_argument("--out", help="write the tables here (default <gate-dir>/evaluation.md)")
    parser.add_argument("--append", action="store_true",
                        help="add the tables after what --out already holds")
    parser.add_argument("--json", help="also write every number as JSON to this file")
    args = parser.parse_args(argv)
    gate_dir = Path(args.gate_dir)
    if args.ideal:
        window = agent_window()
        rows = [evaluate_ideal_task(task, window) for task in TASKS]
        text = render(rows, ideal=True, window=window)
    else:
        rows = [evaluate_gate_task(task, gate_dir) for task in TASKS]
        text = render(rows, ideal=False)
    print(text, end="")
    out = Path(args.out) if args.out else gate_dir / "evaluation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.append and out.exists():
        previous = out.read_text(encoding="utf-8")
        text = previous + ("\n" if previous and not previous.endswith("\n\n") else "") + text
    out.write_text(text, encoding="utf-8")
    print(f"\n{'added to' if args.append else 'wrote'} {out}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
