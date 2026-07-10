from __future__ import annotations

import json
from pathlib import Path


BREAKDOWN_WEIGHTS = {
    "outcome_correctness": 40,
    "constraints": 15,
    "process_quality": 20,
    "code_quality": 15,
    "efficiency": 10,
}


def make_breakdown(passed: bool, overrides: dict | None = None) -> dict:
    if passed:
        breakdown = dict(BREAKDOWN_WEIGHTS)
    else:
        breakdown = {key: 0 for key in BREAKDOWN_WEIGHTS}
    for key, value in (overrides or {}).items():
        if key in BREAKDOWN_WEIGHTS:
            breakdown[key] = max(0, min(BREAKDOWN_WEIGHTS[key], float(value)))
    return breakdown


def emit_result(
    *,
    passed: bool,
    reason: str = "",
    failure_category: str | None = None,
    metrics: dict | None = None,
    breakdown: dict | None = None,
) -> int:
    final_breakdown = make_breakdown(passed, breakdown)
    payload = {
        "passed": passed,
        "score": sum(final_breakdown.values()),
        "breakdown": final_breakdown,
        "metrics": metrics or {},
        "reason": "" if passed else reason,
        "failure_category": None if passed else (failure_category or "grader_error"),
    }
    print(json.dumps(payload))
    return 0 if passed else 1


def trace_events(trace_path: str | Path) -> list[dict]:
    path = Path(trace_path)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def trace_tool_count(trace_path: str | Path) -> int:
    return sum(1 for event in trace_events(trace_path) if event.get("type") == "tool_use")


def trace_contains_test_run(trace_path: str | Path) -> bool:
    for event in trace_events(trace_path):
        if event.get("type") != "tool_use":
            continue
        text = json.dumps(event.get("input", {}), ensure_ascii=False).lower()
        if "pytest" in text or "test" in text:
            return True
    return False
