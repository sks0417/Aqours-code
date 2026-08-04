from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import metrics as eval_metrics
from evals import run_eval


def write_jsonl(path: Path, events: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def make_result(case_dir: Path, *, passed: bool = True) -> dict:
    case_dir.mkdir(parents=True, exist_ok=True)
    trace = case_dir / "trace.jsonl"
    write_jsonl(trace, [])
    metadata = case_dir / "metadata.json"
    metadata.write_text(json.dumps({
        "run_id": "run-1",
        "status": "success",
        "git_commit": "abc123",
        "git_dirty": False,
        "project_version": "0.1.0",
        "model_provider": "deepseek",
        "model": "deepseek-v4-flash",
    }), encoding="utf-8")
    return {
        "case": case_dir.name,
        "passed": passed,
        "score": 100 if passed else 35,
        "failure_category": None if passed else "test_failure",
        "duration_ms": 1200,
        "metrics": {
            "tool_calls": 3,
            "tool_counts": {"read_file": 2, "bash": 1},
            "llm_requests": 2,
            "model_trace_usage_responses": 2,
            "model_trace_usage_missing_responses": 0,
            "model_trace_actual_input_tokens": 100,
            "model_trace_actual_output_tokens": 40,
            "model_trace_actual_total_tokens": 140,
        },
        "trace": str(trace),
        "run_metadata": str(metadata),
        "final": str(case_dir / "final.md"),
        "transcript": str(case_dir / "transcript.md"),
        "change_manifest": str(case_dir / "change_manifest.json"),
        "execution_backend": "local",
        "docker_image": None,
        "forbidden_changes": [],
        "trusted_violations": [],
        "run": {"run_id": "run-1"},
    }


def test_trace_metrics_collects_tools_statuses_and_usage(tmp_path):
    trace = tmp_path / "trace.jsonl"
    write_jsonl(trace, [
        {"type": "llm_request"},
        {"type": "llm_response", "usage": {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        }},
        {"type": "tool_use", "tool": "bash", "input": {
            "command": "python -m pytest -q tests/test_app.py",
        }},
        {"type": "tool_result", "tool": "bash", "status": "failed",
         "exit_code": 1, "content": "1 failed"},
        {"type": "tool_use", "tool": "read_file", "input": {
            "path": "src/app.py",
        }},
        {"type": "tool_use", "tool": "read_file", "input": {
            "path": "src/app.py",
        }},
        {"not": "json"},
    ])
    with trace.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    result = eval_metrics.trace_metrics(trace)

    assert result["tool_calls"] == 3
    assert result["tool_counts"] == {"bash": 1, "read_file": 2}
    assert result["duplicate_tool_calls"] == 1
    assert result["targeted_test_commands"] == 1
    assert result["tool_result_status_counts"] == {"failed": 1}
    assert result["explicit_tool_result_failures"] == 1
    assert result["bash_exit_codes_available"] is True
    assert result["bash_nonzero_exit_codes"] == 1
    assert result["model_trace_actual_input_tokens"] == 10
    assert result["model_trace_actual_output_tokens"] == 5
    assert result["model_trace_actual_total_tokens"] == 15


def test_trial_metrics_uses_grader_as_truth_and_flags_false_success(tmp_path):
    result = make_result(tmp_path / "case-a", passed=False)

    trial = eval_metrics.build_trial_metrics(result)

    assert trial["correctness"] == {
        "grader_passed": False,
        "grader_score": 35,
        "failure_category": "test_failure",
        "trace_status": "success",
        "trace_status_observed": True,
        "false_success": True,
        "constraint_violation": False,
    }
    assert trial["usage"]["source"] == "trace_provider_usage"
    assert trial["usage"]["total_tokens"] == 140
    assert trial["identity"]["model"] == "deepseek-v4-flash"


def test_pass_at_k_requires_enough_trials_and_uses_standard_estimator():
    trials = [
        {"correctness": {"grader_passed": passed}}
        for passed in (True, True, False, False, False)
    ]

    assert eval_metrics.estimate_pass_at_k(trials[:1], 3) is None
    assert eval_metrics.estimate_pass_at_k(trials, 1) == pytest.approx(0.4)
    assert eval_metrics.estimate_pass_at_k(trials, 3) == pytest.approx(0.9)
    assert eval_metrics.estimate_pass_at_k(trials, 5) == 1.0


def test_metrics_artifacts_are_written_beside_case_trace(tmp_path):
    run_root = tmp_path / "runs" / "experiment-1"
    result = make_result(run_root / "case-a")

    paths = eval_metrics.write_metrics_artifacts(
        run_root,
        [result],
        experiment={"mode": "scripted", "execution_backend": "local"},
    )

    assert (run_root / "case-a" / "metrics.json").is_file()
    assert (run_root / "case-a" / "case_summary.json").is_file()
    assert paths["summary"] == run_root / "metrics_summary.json"
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    assert summary["pass_at_1"] == 1.0
    assert summary["pass_at_3"] is None
    assert summary["pass_at_3_eligible_cases"] == 0
    assert summary["actual_total_tokens"] == 140


def test_run_case_automatically_writes_metrics_with_trace_status(tmp_path):
    case = run_eval.PROJECT_ROOT / "evals" / "cases" / "read_file_basic"
    run_root = tmp_path / "runs"

    result = run_eval.run_case(
        case,
        run_root,
        scripted=True,
        execution_config=run_eval.EvalExecutionConfig(backend="local"),
    )

    artifact = Path(result["metrics_artifact"])
    assert artifact == run_root / "read_file_basic" / "metrics.json"
    trial = json.loads(artifact.read_text(encoding="utf-8"))
    assert trial["correctness"]["grader_passed"] is True
    assert trial["correctness"]["trace_status"] == "success"
    assert trial["correctness"]["false_success"] is False


def test_run_eval_summary_automatically_refreshes_metric_artifacts(tmp_path):
    results_dir = tmp_path / "results"
    run_root = results_dir / "runs" / "experiment-1"
    result = make_result(run_root / "case-a")
    summary = {
        "started_at": 1.0,
        "finished_at": 2.0,
        "mode": "scripted",
        "execution_backend": "local",
        "docker_image": None,
        "run_root": str(run_root),
        "results": [result],
    }

    summary_path = run_eval.write_summary(results_dir, summary)

    written = json.loads(summary_path.read_text(encoding="utf-8"))
    assert written["metrics_summary"] == str(
        run_root / "metrics_summary.json"
    )
    assert (run_root / "case-a" / "metrics.json").is_file()
    assert (run_root / "case-a" / "case_summary.json").is_file()
