from __future__ import annotations

from typing import Any


BREAKDOWN_WEIGHTS = {
    "functional_correctness": 50,
    "code_quality": 20,
    "runtime_efficiency": 15,
    "token_cost": 15,
}


DEFAULT_SCORING_PROFILE = {
    "runtime_target_sec": 120.0,
    "runtime_hard_limit_sec": 600.0,
    "llm_call_target": 10.0,
    "llm_call_hard_limit": 40.0,
    "tool_call_target": 25.0,
    "tool_call_hard_limit": 100.0,
    "incident_target": 0.0,
    "incident_hard_limit": 4.0,
    "token_target": 250_000.0,
    "token_hard_limit": 1_500_000.0,
}


PROFILE_METADATA_KEYS = {
    "runtime_target_sec": "score_runtime_target_sec",
    "runtime_hard_limit_sec": "score_runtime_hard_limit_sec",
    "llm_call_target": "score_llm_call_target",
    "llm_call_hard_limit": "score_llm_call_hard_limit",
    "tool_call_target": "score_tool_call_target",
    "tool_call_hard_limit": "score_tool_call_hard_limit",
    "token_target": "score_token_target",
    "token_hard_limit": "score_token_hard_limit",
}


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def lower_is_better_score(
    actual: Any,
    *,
    target: float,
    hard_limit: float,
    weight: float,
) -> float:
    """Return full credit at target and zero at the explicit hard limit."""
    actual_value = max(0.0, _number(actual))
    target = max(0.0, float(target))
    hard_limit = float(hard_limit)
    if hard_limit <= target:
        raise ValueError("hard_limit must be greater than target")
    if actual_value <= target:
        return float(weight)
    if actual_value >= hard_limit:
        return 0.0
    return float(weight) * (hard_limit - actual_value) / (hard_limit - target)


def scoring_profile(metadata: dict | None = None) -> dict[str, float]:
    profile = dict(DEFAULT_SCORING_PROFILE)
    metadata = metadata or {}
    for profile_key, metadata_key in PROFILE_METADATA_KEYS.items():
        if metadata_key in metadata and metadata[metadata_key] is not None:
            profile[profile_key] = _number(
                metadata[metadata_key], profile[profile_key])

    for target_key, hard_key in (
        ("runtime_target_sec", "runtime_hard_limit_sec"),
        ("llm_call_target", "llm_call_hard_limit"),
        ("tool_call_target", "tool_call_hard_limit"),
        ("token_target", "token_hard_limit"),
    ):
        if profile[hard_key] <= profile[target_key]:
            raise ValueError(
                f"{hard_key} must be greater than {target_key}")
    return profile


def _metric(metrics: dict, *names: str, default: float = 0.0) -> float:
    for name in names:
        value = metrics.get(name)
        if value is not None:
            return _number(value, default)
    return default


def operational_scores(
    metrics: dict,
    *,
    metadata: dict | None = None,
    simulated: bool = False,
) -> tuple[dict[str, float], dict]:
    """Score host-observed runtime and Provider usage without changing pass/fail."""
    profile = scoring_profile(metadata)
    runtime_sec = _metric(metrics, "agent_runtime_sec", "runtime_sec")
    llm_calls = _metric(metrics, "trusted_model_calls", "llm_requests")
    tool_calls = _metric(metrics, "tool_calls")
    incidents = sum((
        _metric(metrics, "model_broker_retries"),
        _metric(metrics, "model_broker_provider_errors"),
        _metric(metrics, "permission_blocks"),
        _metric(metrics, "duplicate_tool_calls"),
    ))

    runtime_components = {
        "wall_time": lower_is_better_score(
            runtime_sec,
            target=profile["runtime_target_sec"],
            hard_limit=profile["runtime_hard_limit_sec"],
            weight=6,
        ),
        "llm_calls": lower_is_better_score(
            llm_calls,
            target=profile["llm_call_target"],
            hard_limit=profile["llm_call_hard_limit"],
            weight=4,
        ),
        "tool_calls": lower_is_better_score(
            tool_calls,
            target=profile["tool_call_target"],
            hard_limit=profile["tool_call_hard_limit"],
            weight=3,
        ),
        "reliability": lower_is_better_score(
            incidents,
            target=profile["incident_target"],
            hard_limit=profile["incident_hard_limit"],
            weight=2,
        ),
    }
    runtime_score = sum(runtime_components.values())

    usage_responses = _metric(metrics, "model_broker_usage_responses")
    missing_usage_responses = _metric(
        metrics, "model_broker_usage_missing_responses")
    actual_tokens = metrics.get("model_broker_actual_total_tokens")
    usage_available = actual_tokens is not None and usage_responses > 0
    if usage_available:
        token_score = lower_is_better_score(
            actual_tokens,
            target=profile["token_target"],
            hard_limit=profile["token_hard_limit"],
            weight=BREAKDOWN_WEIGHTS["token_cost"],
        )
        observed_responses = usage_responses + missing_usage_responses
        usage_coverage = (
            usage_responses / observed_responses if observed_responses else 0.0)
        token_score *= usage_coverage
        token_basis = "actual_provider_usage"
    elif simulated:
        token_score = float(BREAKDOWN_WEIGHTS["token_cost"])
        usage_coverage = 1.0
        token_basis = "simulated_unmetered"
    else:
        token_score = 0.0
        usage_coverage = 0.0
        token_basis = "unavailable"

    raw_scores = {
        "runtime_efficiency": round(runtime_score, 3),
        "token_cost": round(token_score, 3),
    }
    details = {
        "version": 2,
        "profile": profile,
        "runtime_components": {
            key: round(value, 3) for key, value in runtime_components.items()
        },
        "raw_operational_points": raw_scores,
        "token_basis": token_basis,
        "usage_available": usage_available,
        "usage_coverage": round(usage_coverage, 4),
        "incidents": incidents,
    }
    return raw_scores, details


def apply_harness_scoring(
    grader_result: dict,
    metrics: dict,
    *,
    metadata: dict | None = None,
    simulated: bool = False,
) -> dict:
    """Overlay host-only operational dimensions while preserving grader pass."""
    result = dict(grader_result)
    grader_breakdown = grader_result.get("breakdown")
    if not isinstance(grader_breakdown, dict):
        grader_breakdown = {}
    functional = max(0.0, min(
        BREAKDOWN_WEIGHTS["functional_correctness"],
        _number(grader_breakdown.get("functional_correctness")),
    ))
    quality = max(0.0, min(
        BREAKDOWN_WEIGHTS["code_quality"],
        _number(grader_breakdown.get("code_quality")),
    ))
    raw_operational, scoring_details = operational_scores(
        metrics, metadata=metadata, simulated=simulated)

    # Cost points should never reward a quick, empty failure. Partial solutions
    # retain proportional credit and raw operational points remain observable.
    correctness_gate = functional / BREAKDOWN_WEIGHTS["functional_correctness"]
    breakdown = {
        "functional_correctness": round(functional, 3),
        "code_quality": round(quality, 3),
        "runtime_efficiency": round(
            raw_operational["runtime_efficiency"] * correctness_gate, 3),
        "token_cost": round(
            raw_operational["token_cost"] * correctness_gate, 3),
    }
    final_metrics = dict(metrics)
    scoring_details["correctness_gate"] = round(correctness_gate, 4)
    scoring_details["dimension_points"] = breakdown
    final_metrics["scoring"] = scoring_details
    final_metrics["dimension_points"] = breakdown
    result["score"] = round(sum(breakdown.values()), 3)
    result["breakdown"] = breakdown
    result["metrics"] = final_metrics
    return result
