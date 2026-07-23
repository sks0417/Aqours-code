#!/usr/bin/env python3
"""Analyze a current CodePilot timeline.jsonl."""
from __future__ import annotations

import argparse
from pathlib import Path

from codepilot_s20.trace_analysis import (
    analyze_events,
    format_counts,
    format_event,
    read_jsonl,
)


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Path to a CodePilot run directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    run_dir = parse_args(argv).run_dir
    print("=== Files in run directory ===")
    try:
        for path in sorted(run_dir.iterdir()):
            print(f"  {path.name}: {path.stat().st_size} bytes")
    except OSError as exc:
        print(f"Unable to list {run_dir}: {exc}")
        return 1

    timeline_path = run_dir / "timeline.jsonl"
    events, issues = read_jsonl(timeline_path)
    print(f"\n=== {timeline_path} ===")
    for index, event in enumerate(events):
        print(format_event(index, event))
    summary = analyze_events(events)
    print("\n=== Aggregate ===")
    print(f"Events: {summary['event_count']}")
    print(f"Event types: {format_counts(summary['event_types'])}")
    print(f"Tools: {format_counts(summary['tools'])}")
    print(f"Errors: {summary['errors']}; permission denials: {summary['permission_denials']}")
    if issues:
        print("Parse issues:")
        for issue in issues:
            print(f"  - {issue}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
