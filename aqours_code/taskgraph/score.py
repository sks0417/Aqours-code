"""Composite score of runs: quality, time and cost against the single-agent baseline.

    S = Q * (T_single / T) ** 0.5 * (C_single / C) ** 0.5

``Q`` is the pass rate of the functional hidden tests times the pass rate of
the regression hidden tests, ``T`` the wall time including planning, and
``C`` the input tokens plus four times the output tokens, including
planning. ``T_single`` and ``C_single`` are the means of the ``single`` runs
of the same task, so the single agent's ``S`` is its own ``Q``.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

BASELINE = "single"
OUTPUT_TOKEN_WEIGHT = 4  # an output token costs about four input tokens
TIME_EXPONENT = 0.5
COST_EXPONENT = 0.5
REGRESSION_DIR = "regression"
RESULT_FILES = ("hidden_tests_rescored.xml", "hidden_tests.xml")  # the first that exists
KINDS = ("functional", "regression")


class ScoreError(Exception):
    """The manifest or one of its files cannot be scored."""


@dataclass(frozen=True)
class Entry:
    """One line of the manifest: a run of ``method`` on ``task``."""

    task: str
    method: str
    run: Path
    plan_report: Path | None = None


def load_manifest(path: Path) -> list[Entry]:
    """Read the manifest; relative paths are relative to its directory."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScoreError(f"cannot read manifest {path}: {exc}") from None
    if not isinstance(data, list) or not data:
        raise ScoreError(f"manifest {path} must be a non-empty list of objects")
    base = Path(path).resolve().parent
    entries = []
    for number, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ScoreError(f"manifest entry {number} is not an object")
        for key in ("task", "method", "run"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ScoreError(f"manifest entry {number} needs a non-empty \"{key}\"")
        report = item.get("plan_report")
        if report is not None and not isinstance(report, str):
            raise ScoreError(f"manifest entry {number}: \"plan_report\" must be a path")
        entries.append(Entry(task=item["task"], method=item["method"],
                             run=base / item["run"],
                             plan_report=base / report if report else None))
    return entries


def load_exclusions(path: Path) -> list[str]:
    """Pytest node ids, one per line; ``#`` starts a comment line."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScoreError(f"cannot read exclusions {path}: {exc}") from None
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def junit_key(node_id: str) -> tuple[str, str]:
    """A node id as JUnit names the test: (dotted module and classes, test name).

    ``a/test_x.py::TestC::test_y[p]`` gives ``("a.test_x.TestC", "test_y[p]")``.
    """
    path, _, rest = node_id.partition("::")
    head, bracket, params = rest.partition("[")
    *classes, name = head.split("::")
    module = path.replace("\\", "/").removesuffix(".py").strip("/").replace("/", ".")
    return ".".join([module, *classes]), name + bracket + params


def excluded_by(classname: str, name: str, exclusions: list[tuple[str, str]]) -> int | None:
    """Index of the exclusion that names this test, ignoring a path prefix."""
    for index, (module, test) in enumerate(exclusions):
        if name == test and (classname == module or classname.endswith("." + module)):
            return index
    return None


def is_regression(case: ET.Element) -> bool:
    """True for a test from the hidden tests' ``regression/`` directory.

    pytest writes ``classname`` as the dotted path of the test file, such as
    ``_hidden_tests.regression.test_utils.test_echo``; some JUnit flavours
    add a ``file`` attribute.
    """
    file = (case.get("file") or "").replace("\\", "/")
    return (REGRESSION_DIR in (case.get("classname") or "").split(".")
            or f"/{REGRESSION_DIR}/" in f"/{file}")


def read_tests(xml_path: Path, exclusions: list[str]) -> dict:
    """Count the tests of one JUnit file by kind and outcome; raises ``ET.ParseError``."""
    keys = [junit_key(node_id) for node_id in exclusions]
    counts = {kind: {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "excluded": 0}
              for kind in KINDS}
    matched = [0] * len(keys)
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        kind = counts["regression" if is_regression(case) else "functional"]
        index = excluded_by(case.get("classname") or "", case.get("name") or "", keys)
        if index is not None:
            matched[index] += 1
            kind["excluded"] += 1
        elif case.find("failure") is not None:
            kind["failed"] += 1
        elif case.find("error") is not None:
            kind["errors"] += 1
        elif case.find("skipped") is not None:  # skip and xfail: not counted
            kind["skipped"] += 1
        else:
            kind["passed"] += 1
    for kind in counts.values():
        counted = kind["passed"] + kind["failed"] + kind["errors"]
        kind["rate"] = kind["passed"] / counted if counted else 1.0
    counts["excluded_matches"] = dict(zip(exclusions, matched))
    return counts


def _load_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScoreError(f"cannot read {what} {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ScoreError(f"{what} {path} is not a JSON object")
    return data


def score_run(entry: Entry, exclusions: list[str]) -> dict:
    """Quality, time and cost of one run; ``valid`` is False when it cannot count."""
    summary = _load_json(entry.run / "summary.json", "summary")
    totals = summary.get("totals") or {}
    time_s = float(summary.get("wall_time_s") or 0.0)
    tokens = {key: int(totals.get(key) or 0) for key in ("input_tokens", "output_tokens")}
    plan = None
    if entry.plan_report is not None:
        report = _load_json(entry.plan_report, "plan report")
        plan_totals = report.get("totals") or {}
        plan = {"report": str(entry.plan_report),
                "duration_s": float(report.get("duration_s") or 0.0),
                **{key: int(plan_totals.get(key) or 0) for key in tokens}}
        time_s += plan["duration_s"]
        for key in tokens:
            tokens[key] += plan[key]
    result = {
        "task": entry.task, "method": entry.method, "run": str(entry.run),
        "run_id": summary.get("run_id") or entry.run.name, "valid": True,
        "invalid_reason": "", "plan": plan, "time_s": time_s, **tokens,
        "cost": tokens["input_tokens"] + OUTPUT_TOKEN_WEIGHT * tokens["output_tokens"],
    }

    def invalid(reason: str) -> dict:
        return {**result, "valid": False, "invalid_reason": reason}

    escapes = int(summary.get("escape_attempts_total") or 0)
    if summary.get("sandbox") == "none" and escapes > 0:
        return invalid(f"--sandbox none with {escapes} escape attempt(s)")
    xml_path = next((entry.run / name for name in RESULT_FILES
                     if (entry.run / name).is_file()), None)
    if xml_path is None:
        return invalid("no hidden test results (hidden_tests.xml)")
    try:
        tests = read_tests(xml_path, exclusions)
    except ET.ParseError as exc:
        return invalid(f"{xml_path.name} cannot be read: {exc}")
    if result["time_s"] <= 0 or result["cost"] <= 0:
        return invalid("summary.json has no time or token counts")
    return {**result, "results_file": xml_path.name, **{kind: tests[kind] for kind in KINDS},
            "excluded_matches": tests["excluded_matches"],
            "q": tests["functional"]["rate"] * tests["regression"]["rate"]}


def composite(q: float, time_s: float, cost: float, base_time: float,
              base_cost: float) -> float:
    """``S`` of one run or group against the single-agent baseline."""
    return (q * (base_time / time_s) ** TIME_EXPONENT
            * (base_cost / cost) ** COST_EXPONENT)


def _spread(values: list[float]) -> dict:
    return {"mean": fmean(values), "min": min(values), "max": max(values)}


def score_manifest(entries: list[Entry], exclusions: list[str] | None = None) -> dict:
    """Score every run and combine them by task and method.

    A group's figures are the means of its valid runs, and its ``S`` is
    computed from those means, so the baseline's ``S`` equals its ``Q``;
    ``s_runs`` gives the spread of the single runs' own ``S``.
    """
    exclusions = exclusions or []
    runs = [score_run(entry, exclusions) for entry in entries]
    valid = [run for run in runs if run["valid"]]
    tasks = list(dict.fromkeys(run["task"] for run in runs))
    methods = list(dict.fromkeys(run["method"] for run in runs))
    detail: dict[str, dict[str, dict]] = {}
    for task in tasks:
        groups = {method: [run for run in valid
                           if (run["task"], run["method"]) == (task, method)]
                  for method in methods}
        if not groups.get(BASELINE):
            raise ScoreError(f"task {task} has no valid \"{BASELINE}\" run to compare with")
        base_time = fmean(run["time_s"] for run in groups[BASELINE])
        base_cost = fmean(run["cost"] for run in groups[BASELINE])
        detail[task] = {}
        for method, group in groups.items():
            if not group:
                continue
            for run in group:
                run["s"] = composite(run["q"], run["time_s"], run["cost"],
                                     base_time, base_cost)
            means = {key: _spread([float(run[key]) for run in group])
                     for key in ("q", "time_s", "cost", "input_tokens", "output_tokens")}
            for kind in KINDS:
                means[f"{kind}_rate"] = _spread([run[kind]["rate"] for run in group])
            detail[task][method] = {
                "runs": len(group), "run_ids": [run["run_id"] for run in group], **means,
                "s": composite(means["q"]["mean"], means["time_s"]["mean"],
                               means["cost"]["mean"], base_time, base_cost),
                "s_runs": _spread([run["s"] for run in group]),
            }
    best = {task: max(groups, key=lambda method: groups[method]["s"])
            for task, groups in detail.items()}
    by_method = {}
    for method in methods:
        scored = [groups[method] for groups in detail.values() if method in groups]
        if scored:
            by_method[method] = {
                "tasks": len(scored),
                "mean_q": fmean(group["q"]["mean"] for group in scored),
                "mean_s": fmean(group["s"] for group in scored),
                "best_on": [task for task in tasks if best[task] == method]}
    matches: dict[str, int] = {node_id: 0 for node_id in exclusions}
    for run in valid:
        for node_id, count in run["excluded_matches"].items():
            matches[node_id] += count
    return {
        "formula": "S = Q * (T_single / T) ** 0.5 * (C_single / C) ** 0.5",
        "baseline": BASELINE, "output_token_weight": OUTPUT_TOKEN_WEIGHT,
        "tasks": detail, "methods": by_method, "best": best,
        "oracle": {"mean_s": fmean(detail[task][best[task]]["s"] for task in tasks),
                   "choice": best},
        "invalid_runs": [{key: run[key] for key in ("task", "method", "run", "invalid_reason")}
                         for run in runs if not run["valid"]],
        "excluded_tests": matches,
        "runs": runs,
    }


def _cell(spread: dict, fmt: str, runs: int) -> str:
    text = format(spread["mean"], fmt)
    if runs > 1:
        text += f" ({format(spread['min'], fmt)}–{format(spread['max'], fmt)})"
    return text


def format_score(result: dict) -> str:
    """The score as Markdown: per task, per method, and the oracle."""
    lines = ["# Score", "", f"`{result['formula']}`, with Q = functional pass rate × "
             f"regression pass rate, T = wall time + planning time, and C = input tokens + "
             f"{result['output_token_weight']} × output tokens (planning included). "
             f"T_single and C_single are the means of the `{result['baseline']}` runs of the "
             "same task. With several runs, a row shows the mean and (min–max); its S is "
             "computed from the means, and the range is that of the single runs' S.", "",
             "## By task", "",
             "| Task | Method | Runs | Functional | Regression | Q | T (s) | Input tokens | "
             "Output tokens | C | S |", "|" + " --- |" * 11]
    for task, groups in result["tasks"].items():
        for method, group in groups.items():
            runs = group["runs"]
            s_text = format(group["s"], ".3f")
            if runs > 1:
                s_text += (f" ({group['s_runs']['min']:.3f}–{group['s_runs']['max']:.3f})")
            lines.append("| " + " | ".join([
                task, method, str(runs), _cell(group["functional_rate"], ".3f", runs),
                _cell(group["regression_rate"], ".3f", runs), _cell(group["q"], ".3f", runs),
                _cell(group["time_s"], ",.0f", runs), _cell(group["input_tokens"], ",.0f", runs),
                _cell(group["output_tokens"], ",.0f", runs), _cell(group["cost"], ",.0f", runs),
                s_text]) + " |")
    lines += ["", "## By method", "", "| Method | Tasks | Mean Q | Mean S | Best S on |",
              "| --- | --- | --- | --- | --- |"]
    for method, row in result["methods"].items():
        lines.append(f"| {method} | {row['tasks']} | {row['mean_q']:.3f} | "
                     f"{row['mean_s']:.3f} | {', '.join(row['best_on']) or '-'} |")
    choice = ", ".join(f"{task}: {method}" for task, method in result["oracle"]["choice"].items())
    lines += ["", "## Oracle", "", "Taking the method with the highest S on each task "
              f"({choice}): mean S = {result['oracle']['mean_s']:.3f}."]
    if result["excluded_tests"]:
        lines += ["", "## Excluded tests", ""]
        lines += [f"- `{node_id}`: {count} result(s) left out"
                  for node_id, count in result["excluded_tests"].items()]
    if result["invalid_runs"]:
        lines += ["", "## Invalid runs (not counted)", ""]
        lines += [f"- {run['task']} / {run['method']}: `{run['run']}`: {run['invalid_reason']}"
                  for run in result["invalid_runs"]]
    return "\n".join(lines) + "\n"


def write_score(result: dict, out_dir: Path) -> None:
    """Write ``score.md`` and ``score.json`` to ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "score.md").write_text(format_score(result), encoding="utf-8", newline="\n")
    (out_dir / "score.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        newline="\n")
