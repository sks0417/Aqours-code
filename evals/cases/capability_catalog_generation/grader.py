from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grader_common import emit_result, trace_tool_count


EXPECTED_ROWS = [
    {"id": "A-100", "title": "Account bootstrap", "priority": "high"},
    {"id": "B-200", "title": "Billing retry policy", "priority": "medium"},
    {"id": "C-300", "title": "Cache warmer cleanup", "priority": "low"},
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--final", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    args = parser.parse_args()

    workspace = Path(args.workspace)
    catalog = workspace / "catalog.csv"
    try:
        rows = list(csv.DictReader(catalog.read_text(encoding="utf-8").splitlines()))
    except Exception as exc:
        rows = []
        read_error = f"{type(exc).__name__}: {exc}"
    else:
        read_error = ""
    marker_errors = []
    for row in EXPECTED_ROWS:
        marker = workspace / "processed" / f"{row['id']}.done"
        if not marker.exists():
            marker_errors.append(f"missing {marker.name}")
            continue
        if marker.read_text(encoding="utf-8").strip() != "processed":
            marker_errors.append(f"bad content in {marker.name}")
    markers_ok = not marker_errors
    passed = rows == EXPECTED_ROWS and markers_ok
    if passed:
        error = ""
    elif read_error:
        error = f"Could not read catalog.csv: {read_error}"
    elif rows != EXPECTED_ROWS:
        error = f"catalog.csv rows mismatch: {rows!r}"
    else:
        error = "; ".join(marker_errors)
    return emit_result(
        passed=passed,
        reason=error,
        failure_category="test_failure",
        metrics={"tool_calls": trace_tool_count(args.trace), "row_count": len(rows)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
