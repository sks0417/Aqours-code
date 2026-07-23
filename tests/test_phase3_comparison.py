from __future__ import annotations

import json

from evals.compare_phase3 import compare_phase3


def write_summary(path, *, reads, tokens, passed=True):
    path.write_text(json.dumps({
        "results": [{
            "case": "stress_distributed_ledger_recovery",
            "passed": passed,
            "metrics": {
                "read_file_calls": reads,
                "model_broker_actual_total_tokens": tokens,
            },
        }],
    }), encoding="utf-8")


def test_phase3_paired_comparison_enforces_all_exit_criteria(tmp_path):
    baselines = []
    candidates = []
    for index, (reads, tokens) in enumerate(((60, 400_000), (54, 360_000))):
        path = tmp_path / f"baseline-{index}.json"
        write_summary(path, reads=reads, tokens=tokens)
        baselines.append(path)
    for index, (reads, tokens) in enumerate(((42, 300_000), (40, 290_000))):
        path = tmp_path / f"candidate-{index}.json"
        write_summary(path, reads=reads, tokens=tokens)
        candidates.append(path)

    result = compare_phase3(baselines, candidates)

    assert result["candidate_median_read_file_calls"] == 41
    assert result["candidate_pass_rate"] == result["baseline_pass_rate"] == 1
    assert result["token_reduction"] >= 0.15
    assert result["phase3_exit_passed"] is True


def test_phase3_comparison_does_not_pass_without_metered_tokens(tmp_path):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_summary(baseline, reads=50, tokens=None)
    write_summary(candidate, reads=40, tokens=None)

    result = compare_phase3([baseline], [candidate])

    assert result["token_usage_comparable"] is False
    assert result["criteria"]["token_reduction_at_least_15_percent"] is False
    assert result["phase3_exit_passed"] is False


def test_phase3_comparison_reads_persisted_run_directories(tmp_path):
    def write_run(name, *, reads, tokens, passed):
        case_dir = (
            tmp_path / name / "stress_distributed_ledger_recovery"
        )
        case_dir.mkdir(parents=True)
        (case_dir / "grader_stdout.txt").write_text(
            json.dumps({"passed": passed}) + "\n",
            encoding="utf-8",
        )
        events = [
            *[
                {
                    "type": "tool_use",
                    "tool": "read_file",
                    "input": {"path": f"file-{index}.py"},
                }
                for index in range(reads)
            ],
            {
                "type": "llm_response",
                "usage": {"total_tokens": tokens},
            },
        ]
        (case_dir / "trace.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events),
            encoding="utf-8",
        )
        return case_dir.parent

    baseline = write_run(
        "baseline", reads=60, tokens=400_000, passed=False,
    )
    candidate = write_run(
        "candidate", reads=40, tokens=300_000, passed=True,
    )

    result = compare_phase3([baseline], [candidate])

    assert result["baseline_median_read_file_calls"] == 60
    assert result["candidate_median_read_file_calls"] == 40
    assert result["token_reduction"] == 0.25
    assert result["phase3_exit_passed"] is True
