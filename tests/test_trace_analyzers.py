from __future__ import annotations

import json

import analyze_timeline
import analyze_trace
from codepilot_s20.trace_analysis import (
    analyze_events,
    post_compact_redundant_reads,
    read_jsonl,
    safe_preview,
)


def _write_jsonl(path, rows, malformed=False):
    lines = [json.dumps(row) for row in rows]
    if malformed:
        lines.insert(1, "{broken")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_current_trace_schema_reports_results_compact_and_repeated_reads(tmp_path):
    rows = [
        {"type": "tool_use", "tool": "read_file", "input": {"path": "src/a.py"}},
        {"type": "tool_result", "tool": "read_file", "status": "success",
         "content": "alpha"},
        {"type": "tool_use", "tool": "read_file", "input": {"path": "src/a.py"}},
        {"type": "tool_use", "tool": "bash",
         "input": {"command": "python -m pytest -q"}},
        {"type": "context_compact", "stage": "micro_compact"},
        {"type": "compact", "kind": "automatic"},
        {"type": "permission_blocked", "reason": "denied"},
    ]
    path = tmp_path / "trace.jsonl"
    _write_jsonl(path, rows, malformed=True)

    events, issues = read_jsonl(path)
    summary = analyze_events(events)

    assert len(issues) == 1 and "line 2" in issues[0]
    assert summary["tools"] == {"bash": 1, "read_file": 2}
    assert summary["repeated_read_paths"] == {"src/a.py": 2}
    assert summary["compact_events"] == 2
    assert summary["permission_denials"] == 1
    assert summary["test_commands"] == ["python -m pytest -q"]


def test_analyzers_use_content_status_and_report_malformed_json(tmp_path, capsys):
    rows = [
        {"type": "tool_result", "tool": "read_file", "status": "failed",
         "content": "current-schema-output"},
    ]
    _write_jsonl(tmp_path / "trace.jsonl", rows, malformed=True)
    _write_jsonl(tmp_path / "timeline.jsonl", rows, malformed=True)

    assert analyze_trace.main([str(tmp_path)]) == 1
    trace_output = capsys.readouterr().out
    assert "status=failed" in trace_output
    assert "size=21" in trace_output
    assert "malformed JSON" in trace_output

    assert analyze_timeline.main([str(tmp_path)]) == 1
    timeline_output = capsys.readouterr().out
    assert "tool=read_file" in timeline_output
    assert "status=failed" in timeline_output
    assert "malformed JSON" in timeline_output


def test_safe_preview_redacts_common_secret_assignments():
    preview = safe_preview("Authorization: bearer-value\napi_key=abc123")
    assert "bearer-value" not in preview
    assert "abc123" not in preview
    assert preview.count("[REDACTED]") == 2


def test_post_compact_redundant_read_metric_uses_digest_and_overlap():
    events = [
        {"type": "read_observation", "path": "src/a.py", "digest": "d1",
         "range_start": 0, "range_end": 200, "limit": None,
         "compact_generation": 0},
        # Same path/digest and overlap after compact: redundant.
        {"type": "read_observation", "path": "src\\a.py", "digest": "d1",
         "range_start": 100, "range_end": 250, "limit": 150,
         "compact_generation": 1},
        # Precise edit read is intentionally excluded.
        {"type": "read_observation", "path": "src/a.py", "digest": "d1",
         "range_start": 120, "range_end": 150, "limit": 30,
         "compact_generation": 1},
        # A modified digest is a new version.
        {"type": "read_observation", "path": "src/a.py", "digest": "d2",
         "range_start": 0, "range_end": 200, "limit": None,
         "compact_generation": 1},
        # A non-overlapping range is useful new information.
        {"type": "read_observation", "path": "src/a.py", "digest": "d1",
         "range_start": 300, "range_end": 400, "limit": 100,
         "compact_generation": 1},
    ]

    result = post_compact_redundant_reads(events)

    assert result["count"] == 1
    assert result["details"][0]["range_start"] == 100
