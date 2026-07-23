#!/usr/bin/env python3
"""Analyze a current CodePilot trace.jsonl and its optional timeline."""
from __future__ import annotations

import argparse
from pathlib import Path

from codepilot_s20.trace_analysis import (
    analyze_events,
    format_counts,
    format_event,
    read_jsonl,
    safe_preview,
)


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Path to a CodePilot run directory")
    return parser.parse_args(argv)


def _print_summary(events: list[dict], issues: list[str]):
    summary = analyze_events(events)
    print("\n=== Aggregate ===")
    print(f"Events: {summary['event_count']}")
    print(f"Event types: {format_counts(summary['event_types'])}")
    print(f"Tools: {format_counts(summary['tools'])}")
    print(f"Compact events: {summary['compact_events']}")
    print(f"Errors: {summary['errors']}; permission denials: {summary['permission_denials']}")
    print(f"Repeated reads: {format_counts(summary['repeated_read_paths'])}")
    if summary["test_commands"]:
        print("Test commands:")
        for command in summary["test_commands"]:
            print(f"  - {safe_preview(command, 180)}")
    if issues:
        print("Parse issues:")
        for issue in issues:
            print(f"  - {issue}")


def main(argv: list[str] | None = None) -> int:
    run_dir = parse_args(argv).run_dir
    trace_path = run_dir / "trace.jsonl"
    events, issues = read_jsonl(trace_path)
    print(f"=== {trace_path} ===")
    for index, event in enumerate(events):
        print(format_event(index, event))
    _print_summary(events, issues)

    timeline_path = run_dir / "timeline.jsonl"
    if timeline_path.is_file():
        timeline_events, timeline_issues = read_jsonl(timeline_path)
        print(f"\nTimeline: {len(timeline_events)} events, {len(timeline_issues)} parse issue(s)")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
