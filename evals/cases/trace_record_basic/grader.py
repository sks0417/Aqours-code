from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import emit_result, trace_tool_count


def load_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args()

    trace_path = Path(args.trace)
    result_path = Path(args.workspace) / "result.txt"
    events = load_events(trace_path) if trace_path.exists() else []
    event_types = {event.get("type") for event in events}
    required = {"user_prompt", "llm_request", "llm_response", "tool_use", "tool_result", "final_answer"}
    missing = sorted(required - event_types)
    has_model_message = any(event.get("type") == "llm_response" and "content" in event for event in events)
    result_ok = result_path.exists() and result_path.read_text(encoding="utf-8").strip() == "trace ok"
    passed = not missing and has_model_message and result_ok
    error = ""
    if missing:
        error = f"Trace missing event types: {', '.join(missing)}"
    elif not has_model_message:
        error = "Trace has no model response content"
    elif not result_ok:
        error = "result.txt was not written with expected content"
    return emit_result(
        passed=passed,
        reason=error,
        failure_category="grader_error" if missing else "test_failure",
        metrics={"tool_calls": trace_tool_count(args.trace),
                 "trace_event_types": sorted(event_types),
                 "trace_missing_types": missing,
                 "trace_has_model_message": has_model_message},
    )


if __name__ == "__main__":
    raise SystemExit(main())
