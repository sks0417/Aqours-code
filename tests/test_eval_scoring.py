from __future__ import annotations

import pytest

from evals.scoring import (
    apply_harness_scoring,
    lower_is_better_score,
    operational_scores,
)


STRESS_PROFILE = {
    "score_runtime_target_sec": 180,
    "score_runtime_hard_limit_sec": 600,
    "score_llm_call_target": 12,
    "score_llm_call_hard_limit": 40,
    "score_tool_call_target": 30,
    "score_tool_call_hard_limit": 100,
    "score_token_target": 300_000,
    "score_token_hard_limit": 1_500_000,
}


def test_lower_is_better_score_is_continuous_between_explicit_bounds():
    assert lower_is_better_score(
        10, target=10, hard_limit=30, weight=6) == 6
    assert lower_is_better_score(
        20, target=10, hard_limit=30, weight=6) == 3
    assert lower_is_better_score(
        30, target=10, hard_limit=30, weight=6) == 0


def test_pass_is_independent_from_continuous_cost_score():
    grader = {
        "passed": True,
        "score": 70,
        "breakdown": {
            "functional_correctness": 50,
            "code_quality": 20,
            "runtime_efficiency": 0,
            "token_cost": 0,
        },
    }
    metrics = {
        "agent_runtime_sec": 438,
        "trusted_model_calls": 39,
        "tool_calls": 59,
        "model_broker_actual_total_tokens": 900_000,
        "model_broker_usage_responses": 39,
        "model_broker_usage_missing_responses": 0,
    }

    result = apply_harness_scoring(
        grader, metrics, metadata=STRESS_PROFILE)

    assert result["passed"] is True
    assert 70 < result["score"] < 100
    assert result["breakdown"]["runtime_efficiency"] < 15
    assert result["breakdown"]["token_cost"] < 15
    assert result["metrics"]["scoring"]["token_basis"] == "actual_provider_usage"


def test_operational_points_are_correctness_gated_to_avoid_fast_failure_reward():
    grader = {
        "passed": False,
        "score": 35,
        "breakdown": {
            "functional_correctness": 25,
            "code_quality": 10,
            "runtime_efficiency": 0,
            "token_cost": 0,
        },
    }
    metrics = {
        "agent_runtime_sec": 1,
        "llm_requests": 1,
        "tool_calls": 1,
    }

    result = apply_harness_scoring(
        grader, metrics, metadata=STRESS_PROFILE, simulated=True)

    assert result["passed"] is False
    assert result["breakdown"]["runtime_efficiency"] == 7.5
    assert result["breakdown"]["token_cost"] == 7.5
    assert result["metrics"]["scoring"]["raw_operational_points"] == {
        "runtime_efficiency": 15,
        "token_cost": 15,
    }


def test_missing_real_provider_usage_gets_no_token_points():
    scores, details = operational_scores(
        {
            "agent_runtime_sec": 10,
            "trusted_model_calls": 1,
            "tool_calls": 2,
            "model_broker_usage_responses": 0,
        },
        metadata=STRESS_PROFILE,
    )

    assert scores["token_cost"] == 0
    assert details["token_basis"] == "unavailable"
    assert details["usage_available"] is False


def test_invalid_profile_rejects_inverted_bounds():
    with pytest.raises(ValueError, match="hard_limit"):
        operational_scores(
            {},
            metadata={
                **STRESS_PROFILE,
                "score_runtime_target_sec": 600,
                "score_runtime_hard_limit_sec": 180,
            },
        )
