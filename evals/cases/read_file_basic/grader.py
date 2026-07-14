from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import emit_result, trace_tool_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args()

    # The final response is the outcome for this read-only task. Trace/stdout/
    # stderr are writable process diagnostics and cannot supply missing facts.
    text = (Path(args.final).read_text(encoding="utf-8", errors="replace")
            if Path(args.final).exists() else "")
    missing = [token for token in ("ALPHA-42", "Eval Systems", "September") if token not in text]
    passed = not missing
    return emit_result(
        passed=passed,
        reason="" if passed else f"Missing expected content: {', '.join(missing)}",
        failure_category="test_failure",
        metrics={"untrusted_agent_tool_calls": trace_tool_count(args.trace)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
