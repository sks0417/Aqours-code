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

    text = "\n".join(
        Path(path).read_text(encoding="utf-8", errors="replace")
        for path in (args.trace, args.final, args.stdout, args.stderr)
        if Path(path).exists()
    )
    missing = [token for token in ("ALPHA-42", "Eval Systems", "September") if token not in text]
    passed = not missing
    return emit_result(
        passed=passed,
        reason="" if passed else f"Missing expected content: {', '.join(missing)}",
        failure_category="test_failure",
        metrics={"tool_calls": trace_tool_count(args.trace)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
