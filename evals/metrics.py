from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path

from aqours_code.trace_analysis import post_compact_redundant_reads


METRICS_SCHEMA_VERSION = 1
FAILED_TOOL_STATUSES = {"blocked", "error", "failed", "timeout"}


def read_trace_events(trace_path: Path) -> list[dict]:
    """Read valid JSON objects from an append-only Trace file."""
    events = []
    if not trace_path.exists():
        return events
    for line in trace_path.read_text(
        encoding="utf-8", errors="replace",
    ).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _usage_total(events: list[dict], *names: str) -> int:
    total = 0
    for event in events:
        if event.get("type") != "llm_response":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        for name in names:
            value = usage.get(name)
            if value is not None:
                total += int(value or 0)
                break
    return total


def trace_metrics(trace_path: Path) -> dict:
    """Compute deterministic per-Trace behavioral and usage metrics."""
    events = read_trace_events(trace_path)
    duplicate_tool_calls = 0
    previous_tool_signature = None
    tool_counts = Counter()
    test_commands = []
    tool_result_statuses = Counter()
    bash_exit_codes = []

    for event in events:
        event_type = event.get("type")
        if event_type == "tool_use":
            tool_name = str(event.get("tool") or event.get("name") or "")
            tool_counts[tool_name] += 1
            tool_input = (
                event.get("input")
                if isinstance(event.get("input"), dict) else {}
            )
            signature = (
                tool_name,
                json.dumps(tool_input, sort_keys=True, ensure_ascii=False),
            )
            if signature == previous_tool_signature:
                duplicate_tool_calls += 1
            previous_tool_signature = signature
            if tool_name == "bash":
                command = str(tool_input.get("command") or "").strip()
                if any(marker in command for marker in (
                    "pytest", "unittest", "python -m test",
                )):
                    test_commands.append(command)
        elif event_type == "tool_result":
            status = str(event.get("status") or "unknown").lower()
            tool_result_statuses[status] += 1
            if str(event.get("tool") or "") == "bash":
                exit_code = event.get("exit_code")
                if isinstance(exit_code, int):
                    bash_exit_codes.append(exit_code)

    redundant_reads = post_compact_redundant_reads(events)
    test_command_counts = Counter(test_commands)
    integrity_events = [
        event for event in events
        if event.get("type") == "context_integrity"
    ]
    context_configuration = next((
        event for event in events
        if event.get("type") == "context_configuration"
    ), {})
    usage_responses = sum(
        1 for event in events
        if event.get("type") == "llm_response"
        and isinstance(event.get("usage"), dict)
        and bool(event.get("usage"))
    )
    llm_responses = sum(
        1 for event in events if event.get("type") == "llm_response"
    )
    verification_starts = [
        event for event in events
        if event.get("type") == "verification_start"
    ]
    verification_results = [
        event for event in events
        if event.get("type") == "verification_result"
    ]
    verification_skips = [
        event for event in events
        if event.get("type") == "verification_skipped"
    ]
    verification_result = (
        verification_results[-1] if verification_results else {}
    )
    verification_skip = (
        verification_skips[-1] if verification_skips else {}
    )

    return {
        "tool_calls": sum(tool_counts.values()),
        "llm_requests": sum(
            1 for event in events if event.get("type") == "llm_request"),
        "llm_responses": llm_responses,
        "permission_blocks": sum(
            1 for event in events
            if event.get("type") == "hook"
            and event.get("name") == "PreToolUse"
            and event.get("decision") == "blocked"
        ),
        "duplicate_tool_calls": duplicate_tool_calls,
        "tool_counts": dict(sorted(tool_counts.items())),
        "tool_results": sum(tool_result_statuses.values()),
        "tool_result_status_counts": dict(sorted(tool_result_statuses.items())),
        "explicit_tool_result_failures": sum(
            tool_result_statuses.get(status, 0)
            for status in FAILED_TOOL_STATUSES
        ),
        "bash_exit_codes_available": bool(bash_exit_codes),
        "bash_exit_code_results": len(bash_exit_codes),
        "bash_nonzero_exit_codes": sum(
            1 for exit_code in bash_exit_codes if exit_code != 0
        ),
        "read_file_calls": tool_counts.get("read_file", 0),
        "post_compact_redundant_reads": redundant_reads["count"],
        "post_compact_redundant_read_details": redundant_reads["details"],
        "automatic_compactions": sum(
            1 for event in events
            if event.get("type") == "compact"
            and event.get("kind") == "automatic"
            and event.get("success") is True
        ),
        "oversized_tool_results_omitted": sum(
            int(event.get("omitted_tool_result_count") or 0)
            for event in events
            if event.get("type") == "context_compact"
            and event.get("stage") == "tool_result_limit"
        ),
        "exact_repeated_test_commands": sum(
            count - 1 for count in test_command_counts.values() if count > 1
        ),
        "targeted_test_commands": sum(
            1 for command in test_commands
            if "::" in command or " -k " in f" {command} "
            or "test_" in command
        ),
        "context_integrity_events": len(integrity_events),
        "context_integrity_failures": sum(
            1 for event in integrity_events
            if (not event.get("checkpoint_present")
                or not event.get("latest_user_preserved")
                or bool(event.get("orphan_tool_result_ids")))
        ),
        "context_configuration": {
            "context_limit_chars": context_configuration.get(
                "context_limit_chars"),
            "compact_trigger_ratio": context_configuration.get(
                "compact_trigger_ratio"),
            "eval_override": bool(context_configuration.get("eval_override")),
        },
        "team_event_counts": dict(sorted(Counter(
            str(event.get("type")) for event in events
            if str(event.get("type", "")).startswith((
                "shared_task_", "message_bus_", "worktree_", "teammate_",
            ))
        ).items())),
        "model_trace_usage_responses": usage_responses,
        "model_trace_usage_missing_responses": llm_responses - usage_responses,
        "model_trace_actual_input_tokens": _usage_total(
            events, "input_tokens", "prompt_tokens",
        ),
        "model_trace_actual_output_tokens": _usage_total(
            events, "output_tokens", "completion_tokens",
        ),
        "model_trace_actual_cache_creation_input_tokens": _usage_total(
            events, "cache_creation_input_tokens",
        ),
        "model_trace_actual_cache_read_input_tokens": _usage_total(
            events, "cache_read_input_tokens",
        ),
        "model_trace_actual_total_tokens": _usage_total(events, "total_tokens"),
        "verifier_invoked": bool(verification_starts),
        "verifier_status": verification_result.get("status"),
        "verifier_model_calls": int(
            verification_result.get("model_calls") or 0),
        "verifier_tool_calls": int(
            verification_result.get("tool_calls") or 0),
        "verifier_tests_run": int(
            verification_result.get("tests_run") or 0),
        "verifier_findings_found": int(
            verification_result.get("findings_found") or 0),
        "verifier_blockers_found": int(
            verification_result.get("blockers_found") or 0),
        "verifier_allocated_model_calls": (
            int(verification_result.get("allocated_model_calls"))
            if verification_result.get("allocated_model_calls") is not None
            else None
        ),
        "verifier_workspace_modified": bool(
            verification_result.get("workspace_modified")),
        "verifier_skipped_reason": (
            None if verification_starts else verification_skip.get(
                "verification_skipped_reason")
        ),
        "event_count": len(events),
    }


def _read_json(path_value) -> dict:
    if not path_value:
        return {}
    try:
        value = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _token_metrics(metrics: dict) -> dict:
    broker_usage = int(metrics.get("model_broker_usage_responses") or 0)
    if broker_usage:
        return {
            "source": "model_broker_provider_usage",
            "metered_responses": broker_usage,
            "missing_responses": int(
                metrics.get("model_broker_usage_missing_responses") or 0
            ),
            "input_tokens": _number(
                metrics.get("model_broker_actual_input_tokens")),
            "output_tokens": _number(
                metrics.get("model_broker_actual_output_tokens")),
            "cache_creation_input_tokens": _number(metrics.get(
                "model_broker_actual_cache_creation_input_tokens")),
            "cache_read_input_tokens": _number(metrics.get(
                "model_broker_actual_cache_read_input_tokens")),
            "total_tokens": _number(
                metrics.get("model_broker_actual_total_tokens")),
        }

    trace_usage = int(metrics.get("model_trace_usage_responses") or 0)
    if trace_usage:
        return {
            "source": "trace_provider_usage",
            "metered_responses": trace_usage,
            "missing_responses": int(
                metrics.get("model_trace_usage_missing_responses") or 0
            ),
            "input_tokens": _number(
                metrics.get("model_trace_actual_input_tokens")),
            "output_tokens": _number(
                metrics.get("model_trace_actual_output_tokens")),
            "cache_creation_input_tokens": _number(metrics.get(
                "model_trace_actual_cache_creation_input_tokens")),
            "cache_read_input_tokens": _number(metrics.get(
                "model_trace_actual_cache_read_input_tokens")),
            "total_tokens": _number(
                metrics.get("model_trace_actual_total_tokens")),
        }

    return {
        "source": "unavailable",
        "metered_responses": 0,
        "missing_responses": int(
            metrics.get("model_broker_usage_missing_responses") or 0
        ),
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "total_tokens": None,
    }


def build_trial_metrics(result: dict) -> dict:
    """Build the canonical objective metrics document for one Eval trial."""
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    run_metadata = _read_json(result.get("run_metadata"))
    run_info = result.get("run") if isinstance(result.get("run"), dict) else {}
    trace_status = run_metadata.get("status")
    grader_passed = bool(result.get("passed"))
    trace_status_observed = trace_status is not None
    constraint_violation = bool(
        result.get("failure_category") == "constraint_violation"
        or result.get("forbidden_changes")
        or result.get("trusted_violations")
    )

    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "generated_at": time.time(),
        "case_id": str(result.get("case") or "unknown"),
        "trial_id": (
            run_metadata.get("run_id")
            or run_info.get("run_id")
            or result.get("trial_id")
        ),
        "identity": {
            "git_commit": run_metadata.get("git_commit"),
            "git_dirty": run_metadata.get("git_dirty"),
            "project_version": run_metadata.get("project_version"),
            "model_provider": run_metadata.get("model_provider"),
            "model": run_metadata.get("model"),
            "execution_backend": result.get("execution_backend"),
            "docker_image": result.get("docker_image"),
        },
        "correctness": {
            "grader_passed": grader_passed,
            "grader_score": _number(result.get("score")),
            "failure_category": result.get("failure_category"),
            "trace_status": trace_status,
            "trace_status_observed": trace_status_observed,
            "false_success": (
                trace_status == "success" and not grader_passed
                if trace_status_observed else None
            ),
            "constraint_violation": constraint_violation,
        },
        "usage": _token_metrics(metrics),
        "execution": {
            "duration_ms": int(result.get("duration_ms") or 0),
            "agent_runtime_sec": _number(metrics.get("agent_runtime_sec")),
            "llm_requests": int(metrics.get("llm_requests") or 0),
            "trusted_model_calls": _number(metrics.get("trusted_model_calls")),
            "model_retries": int(metrics.get("model_broker_retries") or 0),
            "tool_calls": int(metrics.get("tool_calls") or 0),
            "tool_counts": dict(metrics.get("tool_counts") or {}),
            "tool_results": int(metrics.get("tool_results") or 0),
            "tool_result_status_counts": dict(
                metrics.get("tool_result_status_counts") or {}),
            "explicit_tool_result_failures": int(
                metrics.get("explicit_tool_result_failures") or 0),
            "bash_exit_codes_available": bool(
                metrics.get("bash_exit_codes_available")),
            "bash_nonzero_exit_codes": int(
                metrics.get("bash_nonzero_exit_codes") or 0),
            "permission_blocks": int(metrics.get("permission_blocks") or 0),
            "automatic_compactions": int(
                metrics.get("automatic_compactions") or 0),
            "post_compact_redundant_reads": int(
                metrics.get("post_compact_redundant_reads") or 0),
            "duplicate_tool_calls": int(
                metrics.get("duplicate_tool_calls") or 0),
            "targeted_test_commands": int(
                metrics.get("targeted_test_commands") or 0),
        },
        "artifacts": {
            "trace": result.get("trace"),
            "run_metadata": result.get("run_metadata"),
            "final": result.get("final"),
            "transcript": result.get("transcript"),
            "change_manifest": result.get("change_manifest"),
        },
    }


def estimate_pass_at_k(trials: list[dict], k: int) -> float | None:
    """Estimate pass@k for one case from independent trial outcomes."""
    n = len(trials)
    if k < 1 or n < k:
        return None
    successes = sum(
        1 for trial in trials
        if trial.get("correctness", {}).get("grader_passed") is True
    )
    failures = n - successes
    if failures < k:
        return 1.0
    return 1.0 - (math.comb(failures, k) / math.comb(n, k))


def _average(values) -> float | None:
    values = [value for value in values if _number(value) is not None]
    return sum(values) / len(values) if values else None


def _sum_known(values) -> int | float | None:
    values = [value for value in values if _number(value) is not None]
    return sum(values) if values else None


def build_case_summary(case_id: str, trials: list[dict]) -> dict:
    successes = sum(
        1 for trial in trials
        if trial.get("correctness", {}).get("grader_passed") is True
    )
    false_success_values = [
        trial.get("correctness", {}).get("false_success") for trial in trials
    ]
    tool_counts = Counter()
    for trial in trials:
        tool_counts.update(trial.get("execution", {}).get("tool_counts") or {})
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "case_id": case_id,
        "trial_count": len(trials),
        "success_count": successes,
        "failure_count": len(trials) - successes,
        "pass_at_1": estimate_pass_at_k(trials, 1),
        "pass_at_3": estimate_pass_at_k(trials, 3),
        "pass_at_5": estimate_pass_at_k(trials, 5),
        "all_trials_passed": bool(trials) and successes == len(trials),
        "consistent_3_of_3": (
            all(
                trial.get("correctness", {}).get("grader_passed") is True
                for trial in trials
            ) if len(trials) == 3 else None
        ),
        "trace_status_observed_trials": sum(
            1 for value in false_success_values if value is not None
        ),
        "false_success_count": sum(
            1 for value in false_success_values if value is True
        ),
        "constraint_violation_count": sum(
            1 for trial in trials
            if trial.get("correctness", {}).get("constraint_violation") is True
        ),
        "avg_score": _average(
            trial.get("correctness", {}).get("grader_score")
            for trial in trials
        ),
        "avg_duration_ms": _average(
            trial.get("execution", {}).get("duration_ms") for trial in trials
        ),
        "avg_llm_requests": _average(
            trial.get("execution", {}).get("llm_requests") for trial in trials
        ),
        "avg_tool_calls": _average(
            trial.get("execution", {}).get("tool_calls") for trial in trials
        ),
        "tool_calls_by_name": dict(sorted(tool_counts.items())),
        "token_metered_trials": sum(
            1 for trial in trials
            if trial.get("usage", {}).get("source") != "unavailable"
        ),
        "total_tokens": _sum_known(
            trial.get("usage", {}).get("total_tokens") for trial in trials
        ),
        "avg_total_tokens": _average(
            trial.get("usage", {}).get("total_tokens") for trial in trials
        ),
    }


def build_metrics_summary(
    trials: list[dict], *, run_root: Path, experiment: dict | None = None,
) -> dict:
    grouped = {}
    for trial in trials:
        grouped.setdefault(str(trial.get("case_id") or "unknown"), []).append(trial)
    cases = {
        case_id: build_case_summary(case_id, case_trials)
        for case_id, case_trials in sorted(grouped.items())
    }
    case_summaries = list(cases.values())
    tool_counts = Counter()
    failure_categories = Counter()
    for trial in trials:
        tool_counts.update(trial.get("execution", {}).get("tool_counts") or {})
        category = trial.get("correctness", {}).get("failure_category")
        if category:
            failure_categories[str(category)] += 1

    def macro_pass(k: int) -> tuple[float | None, int]:
        values = [case.get(f"pass_at_{k}") for case in case_summaries]
        eligible = [value for value in values if value is not None]
        return (_average(eligible), len(eligible))

    pass_at_1, pass_at_1_cases = macro_pass(1)
    pass_at_3, pass_at_3_cases = macro_pass(3)
    pass_at_5, pass_at_5_cases = macro_pass(5)
    successes = sum(
        1 for trial in trials
        if trial.get("correctness", {}).get("grader_passed") is True
    )

    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "generated_at": time.time(),
        "run_root": str(run_root),
        "experiment": dict(experiment or {}),
        "case_count": len(cases),
        "trial_count": len(trials),
        "success_count": successes,
        "failure_count": len(trials) - successes,
        "trial_success_rate": successes / len(trials) if trials else 0.0,
        "pass_at_1": pass_at_1,
        "pass_at_3": pass_at_3,
        "pass_at_5": pass_at_5,
        "pass_at_1_eligible_cases": pass_at_1_cases,
        "pass_at_3_eligible_cases": pass_at_3_cases,
        "pass_at_5_eligible_cases": pass_at_5_cases,
        "false_success_count": sum(
            1 for trial in trials
            if trial.get("correctness", {}).get("false_success") is True
        ),
        "constraint_violation_count": sum(
            1 for trial in trials
            if trial.get("correctness", {}).get("constraint_violation") is True
        ),
        "avg_score": _average(
            trial.get("correctness", {}).get("grader_score") for trial in trials
        ),
        "avg_duration_ms": _average(
            trial.get("execution", {}).get("duration_ms") for trial in trials
        ),
        "avg_llm_requests": _average(
            trial.get("execution", {}).get("llm_requests") for trial in trials
        ),
        "avg_tool_calls": _average(
            trial.get("execution", {}).get("tool_calls") for trial in trials
        ),
        "tool_calls_by_name": dict(sorted(tool_counts.items())),
        "token_metered_trials": sum(
            1 for trial in trials
            if trial.get("usage", {}).get("source") != "unavailable"
        ),
        "actual_input_tokens": _sum_known(
            trial.get("usage", {}).get("input_tokens") for trial in trials
        ),
        "actual_output_tokens": _sum_known(
            trial.get("usage", {}).get("output_tokens") for trial in trials
        ),
        "actual_total_tokens": _sum_known(
            trial.get("usage", {}).get("total_tokens") for trial in trials
        ),
        "failure_categories": dict(sorted(failure_categories.items())),
        "cases": cases,
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    temporary.replace(path)
    return path


def write_trial_metrics(result: dict, path: Path | None = None) -> Path:
    trace_path = Path(result.get("trace") or "")
    if path is None:
        if not result.get("trace"):
            raise ValueError("Eval result has no trace path for metrics output")
        path = trace_path.parent / "metrics.json"
    return _write_json(path, build_trial_metrics(result))


def write_metrics_artifacts(
    run_root: Path,
    results: list[dict],
    *,
    experiment: dict | None = None,
) -> dict:
    trials = []
    grouped = {}
    trial_paths = []
    for result in results:
        trial = build_trial_metrics(result)
        trials.append(trial)
        case_id = str(trial.get("case_id") or "unknown")
        grouped.setdefault(case_id, []).append(trial)
        trace_path = Path(result.get("trace") or run_root / case_id / "trace.jsonl")
        trial_paths.append(_write_json(trace_path.parent / "metrics.json", trial))

    case_paths = {}
    for case_id, case_trials in sorted(grouped.items()):
        case_paths[case_id] = _write_json(
            run_root / case_id / "case_summary.json",
            build_case_summary(case_id, case_trials),
        )
    summary_path = _write_json(
        run_root / "metrics_summary.json",
        build_metrics_summary(
            trials, run_root=run_root, experiment=experiment,
        ),
    )
    return {
        "summary": summary_path,
        "cases": case_paths,
        "trials": trial_paths,
    }


def _load_existing_trials(run_root: Path) -> list[dict]:
    trials = []
    for path in sorted(run_root.glob("*/metrics.json")):
        value = _read_json(path)
        if value.get("schema_version") == METRICS_SCHEMA_VERSION:
            trials.append(value)
    return trials


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild Aqours_code Eval metric summaries.",
    )
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args(argv)
    run_root = args.run_root.resolve()
    trials = _load_existing_trials(run_root)
    if not trials:
        parser.error(f"no per-case metrics.json files found under {run_root}")
    grouped = {}
    for trial in trials:
        grouped.setdefault(str(trial.get("case_id") or "unknown"), []).append(trial)
    for case_id, case_trials in grouped.items():
        _write_json(
            run_root / case_id / "case_summary.json",
            build_case_summary(case_id, case_trials),
        )
    summary_path = _write_json(
        run_root / "metrics_summary.json",
        build_metrics_summary(trials, run_root=run_root),
    )
    print(summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
