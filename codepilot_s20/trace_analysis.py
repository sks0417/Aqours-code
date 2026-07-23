from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re


_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)")
_TEST_COMMAND = re.compile(
    r"(?i)(?:^|\s)(?:pytest|py\.test|python\s+-m\s+pytest|"
    r"unittest|npm\s+test|cargo\s+test|go\s+test)(?:\s|$)")


def read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    events: list[dict] = []
    issues: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [], [f"unable to read {path}: {exc}"]
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"line {line_number}: malformed JSON ({exc.msg})")
            continue
        if not isinstance(value, dict):
            issues.append(f"line {line_number}: JSON value is not an object")
            continue
        value.setdefault("_line_number", line_number)
        events.append(value)
    return events, issues


def event_tool(event: dict) -> str:
    tool = event.get("tool") or event.get("toolName")
    if tool:
        return str(tool)
    call = event.get("toolCall")
    return str(call.get("name", "")) if isinstance(call, dict) else ""


def event_content(event: dict) -> str:
    for key in ("content", "summary", "error", "reason", "result", "message"):
        value = event.get(key)
        if value is not None:
            if isinstance(value, str):
                return value
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def event_status(event: dict) -> str:
    status = event.get("status")
    if status:
        return str(status)
    if event.get("isError") is True:
        return "error"
    if event.get("type") == "tool_result":
        return "success"
    return ""


def safe_preview(value: object, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True)
    text = _SECRET.sub(r"\1\2[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:max(0, limit - 3)] + "..."
    return text


def analyze_events(events: list[dict]) -> dict:
    type_counts = Counter(str(event.get("type", "unknown")) for event in events)
    tool_counts = Counter()
    read_paths = Counter()
    test_commands: list[str] = []
    error_count = 0
    permission_denials = 0
    for event in events:
        event_type = str(event.get("type", "unknown"))
        tool = event_tool(event)
        if event_type == "tool_use" and tool:
            tool_counts[tool] += 1
            input_value = event.get("input", {})
            if not isinstance(input_value, dict):
                input_value = {}
            if tool == "read_file" and input_value.get("path"):
                read_paths[str(input_value["path"])] += 1
            command = str(input_value.get("command", ""))
            if tool == "bash" and _TEST_COMMAND.search(command):
                test_commands.append(command)
        status = event_status(event).lower()
        if (event_type == "error"
                or status in {"error", "failed", "failure"}):
            error_count += 1
        if (event_type == "permission_blocked"
                or (event_type == "hook"
                    and str(event.get("decision", "")).lower() == "blocked")):
            permission_denials += 1
    return {
        "event_count": len(events),
        "event_types": dict(sorted(type_counts.items())),
        "tools": dict(sorted(tool_counts.items())),
        "repeated_read_paths": {
            path: count for path, count in sorted(read_paths.items())
            if count > 1
        },
        "test_commands": test_commands,
        "compact_events": (
            type_counts.get("compact", 0)
            + type_counts.get("context_compact", 0)),
        "errors": error_count,
        "permission_denials": permission_denials,
    }


def format_counts(values: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in values.items()) or "(none)"


def format_event(index: int, event: dict) -> str:
    event_type = str(event.get("type", "unknown"))
    timestamp = event.get("ts", event.get("timestamp", ""))
    tool = event_tool(event)
    details = []
    if tool:
        details.append(f"tool={tool}")
    if event_type == "tool_use":
        details.append(f"input={safe_preview(event.get('input', {}))}")
    elif event_type == "tool_result":
        content = event_content(event)
        details.extend((f"status={event_status(event)}", f"size={len(content)}"))
        if content:
            details.append(f"preview={safe_preview(content)}")
    elif event_type in {"error", "permission_blocked", "task_notification"}:
        content = event_content(event)
        if content:
            details.append(f"preview={safe_preview(content)}")
    elif event_type in {"compact", "context_compact"}:
        details.append(f"kind={event.get('kind', event.get('stage', ''))}")
    suffix = " | " + " | ".join(details) if details else ""
    return f"[{index:4d}] {event_type:22s} | ts={timestamp}{suffix}"
