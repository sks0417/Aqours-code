"""Compare paired Eval summaries against the Phase 3 exit criteria."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

try:
    from .run_eval import trace_metrics
except ImportError:
    from run_eval import trace_metrics


DEFAULT_CASE = "stress_distributed_ledger_recovery"


def _json_line(path: Path) -> dict:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"{path} does not contain a JSON object")


def _case_result(summary_path: str | Path, case: str) -> dict:
    path = Path(summary_path)
    if path.is_dir():
        case_dir = path / case if (path / case).is_dir() else path
        trace_path = case_dir / "trace.jsonl"
        grader_path = case_dir / "grader_stdout.txt"
        if not trace_path.is_file() or not grader_path.is_file():
            raise ValueError(
                f"{path} is not an Eval run/case directory for {case}"
            )
        grader = _json_line(grader_path)
        metrics = trace_metrics(trace_path)
        return {
            "summary": str(case_dir.resolve()),
            "passed": bool(grader.get("passed")),
            "read_file_calls": int(metrics.get("read_file_calls", 0)),
            "actual_tokens": metrics.get("model_trace_actual_total_tokens"),
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = next(
        (item for item in payload.get("results", [])
         if item.get("case") == case),
        None,
    )
    if result is None:
        raise ValueError(f"{path} does not contain case {case}")
    metrics = dict(result.get("metrics", {}))
    if "read_file_calls" not in metrics and result.get("trace"):
        metrics.update(trace_metrics(Path(result["trace"])))
    return {
        "summary": str(path.resolve()),
        "passed": bool(result.get("passed")),
        "read_file_calls": int(metrics.get("read_file_calls", 0)),
        "actual_tokens": (
            metrics.get("model_broker_actual_total_tokens")
            or metrics.get("model_trace_actual_total_tokens")
        ),
    }


def compare_phase3(
    baseline_paths,
    candidate_paths,
    *,
    case: str = DEFAULT_CASE,
) -> dict:
    baseline = [_case_result(path, case) for path in baseline_paths]
    candidate = [_case_result(path, case) for path in candidate_paths]
    if not baseline or not candidate:
        raise ValueError("at least one baseline and candidate summary is required")
    baseline_tokens = [
        int(item["actual_tokens"]) for item in baseline
        if item["actual_tokens"] is not None and int(item["actual_tokens"]) > 0
    ]
    candidate_tokens = [
        int(item["actual_tokens"]) for item in candidate
        if item["actual_tokens"] is not None and int(item["actual_tokens"]) > 0
    ]
    token_comparable = bool(baseline_tokens and candidate_tokens)
    baseline_token_median = (
        statistics.median(baseline_tokens) if baseline_tokens else None
    )
    candidate_token_median = (
        statistics.median(candidate_tokens) if candidate_tokens else None
    )
    token_reduction = (
        1 - candidate_token_median / baseline_token_median
        if token_comparable and baseline_token_median else None
    )
    baseline_pass_rate = sum(
        item["passed"] for item in baseline
    ) / len(baseline)
    candidate_pass_rate = sum(
        item["passed"] for item in candidate
    ) / len(candidate)
    criteria = {
        "median_read_file_below_45": (
            statistics.median(
                item["read_file_calls"] for item in candidate
            ) < 45
        ),
        "pass_rate_not_lower": candidate_pass_rate >= baseline_pass_rate,
        "token_reduction_at_least_15_percent": (
            token_comparable and token_reduction >= 0.15
        ),
    }
    return {
        "case": case,
        "baseline_runs": baseline,
        "candidate_runs": candidate,
        "baseline_pass_rate": baseline_pass_rate,
        "candidate_pass_rate": candidate_pass_rate,
        "baseline_median_read_file_calls": statistics.median(
            item["read_file_calls"] for item in baseline
        ),
        "candidate_median_read_file_calls": statistics.median(
            item["read_file_calls"] for item in candidate
        ),
        "baseline_median_actual_tokens": baseline_token_median,
        "candidate_median_actual_tokens": candidate_token_median,
        "token_reduction": token_reduction,
        "token_usage_comparable": token_comparable,
        "criteria": criteria,
        "phase3_exit_passed": all(criteria.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="append", required=True)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--case", default=DEFAULT_CASE)
    args = parser.parse_args()
    result = compare_phase3(
        args.baseline, args.candidate, case=args.case,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["phase3_exit_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
