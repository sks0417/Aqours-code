from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from .config import (
    TRACE_CLEANUP_ENABLED,
    TRACE_KEEP_PINNED,
    TRACE_MAX_RUN_MB,
    TRACE_RETENTION_MAX_DAYS,
    TRACE_RETENTION_MAX_MB,
    TRACE_RETENTION_MAX_RUNS,
)


CURRENT_TRACE = None
MAX_TOOL_RESULT_CHARS = 5000
RUN_STATUSES = {"running", "success", "failed", "blocked", "timeout", "cancelled"}
SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "authorization",
    "bearer",
)


def _json_default(value):
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def _truncate(value, limit: int = MAX_TOOL_RESULT_CHARS):
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _is_sensitive_key(key) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if normalized in {"apikey", "authorization", "bearer"}:
        return True
    return normalized.endswith(("token", "password", "secret"))


def _redact_text(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    key_pattern = r"api[_-]?key|apikey|token|password|secret|authorization|bearer"
    text = re.sub(
        r"(?i)([\"']?\bauthorization\b[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?([^\"'\s,;}]+)([\"']?)",
        r"\1[REDACTED]\3",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+([A-Za-z0-9._~+/\-]+=*)",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        rf"(?i)([\"']?\b(?:{key_pattern})\b[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}}]+)([\"']?)",
        r"\1[REDACTED]\3",
        text,
    )
    return text


def _redact(value):
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            redacted[key] = "[REDACTED]" if _is_sensitive_key(key) else _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        elif getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(part for part in parts if part)


def _result_status(content) -> str:
    text = str(content).strip().lower()
    failed_prefixes = (
        "permission denied",
        "tool not run",
        "error:",
        "[error]",
        "failed",
        "exception",
        "unknown:",
    )
    if text.startswith(failed_prefixes) or "traceback" in text:
        return "failed"
    return "success"


def _format_markdown_value(value) -> str:
    if value in (None, ""):
        text = "(empty)"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)
    else:
        text = str(value)
    return "\n".join(f"    {line}" for line in text.splitlines() or ["(empty)"])


def _timeline_from_trace_event(event: dict):
    event_type = event.get("type")
    if event_type == "user_prompt":
        return {
            "type": "user_prompt",
            "ts": event.get("ts"),
            "prompt": event.get("prompt", ""),
        }
    if event_type == "tool_use":
        return {
            "type": "tool_use",
            "ts": event.get("ts"),
            "tool": event.get("tool", ""),
            "tool_use_id": event.get("tool_use_id", ""),
            "input": event.get("input", {}),
        }
    if event_type == "tool_result":
        content = event.get("content", "")
        return {
            "type": "tool_result",
            "ts": event.get("ts"),
            "tool": event.get("tool", ""),
            "tool_use_id": event.get("tool_use_id", ""),
            "status": _result_status(content),
            "content": content,
        }
    if event_type == "hook":
        if (event.get("name") == "PreToolUse"
                and event.get("decision") == "blocked"):
            if event.get("recoverable"):
                return {
                    "type": "tool_policy_rejected",
                    "ts": event.get("ts"),
                    "tool": event.get("tool", ""),
                    "tool_use_id": event.get("tool_use_id", ""),
                    "input": event.get("input", {}),
                    "reason": event.get("reason", ""),
                }
            return {
                "type": "permission_blocked",
                "ts": event.get("ts"),
                "tool": event.get("tool", ""),
                "tool_use_id": event.get("tool_use_id", ""),
                "input": event.get("input", {}),
                "reason": event.get("reason", ""),
            }
        return None
    if event_type == "background_routed":
        return {
            "type": "background_routed",
            "ts": event.get("ts"),
            "tool_use_id": event.get("tool_use_id", ""),
            "command": event.get("command", ""),
            "reason": event.get("reason", ""),
        }
    if event_type == "task_notification":
        return {
            "type": "task_notification",
            "ts": event.get("ts"),
            "task_id": event.get("task_id", ""),
            "status": event.get("status", ""),
            "command": event.get("command", ""),
            "summary": event.get("summary", ""),
            "injection": event.get("injection", ""),
            "original_size": event.get("original_size", 0),
            "truncated": bool(event.get("truncated", False)),
        }
    if event_type == "error":
        return {
            "type": "error",
            "ts": event.get("ts"),
            "error_type": event.get("error_type", ""),
            "message": event.get("message", ""),
        }
    if event_type == "final_answer":
        return {
            "type": "final_answer",
            "ts": event.get("ts"),
            "content": event.get("content", ""),
        }
    return None


def _render_timeline_markdown(run_id: str, events: list[dict]) -> str:
    lines = ["# Run Timeline", "", f"Run: `{run_id}`", ""]
    if not events:
        lines.append("(No timeline events yet.)")
        lines.append("")
        return "\n".join(lines)

    for event in events:
        event_type = event.get("type")
        if event_type == "user_prompt":
            lines.extend([
                "## User Request",
                "",
                _format_markdown_value(event.get("prompt", "")),
                "",
            ])
        elif event_type == "tool_use":
            lines.extend([
                f"## Tool Call: {event.get('tool', '')}",
                "",
                f"Tool use id: `{event.get('tool_use_id', '')}`",
                "",
                "Input:",
                "",
                _format_markdown_value(event.get("input", {})),
                "",
            ])
        elif event_type == "tool_result":
            lines.extend([
                f"## Tool Result: {event.get('tool', '')}",
                "",
                f"Status: `{event.get('status', '')}`",
                "",
                "Output:",
                "",
                _format_markdown_value(event.get("content", "")),
                "",
            ])
        elif event_type == "permission_blocked":
            lines.extend([
                f"## Permission Blocked: {event.get('tool', '')}",
                "",
                "Input:",
                "",
                _format_markdown_value(event.get("input", {})),
                "",
                "Reason:",
                "",
                _format_markdown_value(event.get("reason", "")),
                "",
            ])
        elif event_type == "tool_policy_rejected":
            lines.extend([
                f"## Tool Policy Rejected: {event.get('tool', '')}",
                "",
                "Input:",
                "",
                _format_markdown_value(event.get("input", {})),
                "",
                "Guidance:",
                "",
                _format_markdown_value(event.get("reason", "")),
                "",
            ])
        elif event_type == "background_routed":
            lines.extend([
                "## Background Task Routed",
                "",
                f"Tool use id: `{event.get('tool_use_id', '')}`",
                "",
                f"Reason: `{event.get('reason', '')}`",
                "",
                "Command:",
                "",
                _format_markdown_value(event.get("command", "")),
                "",
            ])
        elif event_type == "task_notification":
            truncation = "yes" if event.get("truncated") else "no"
            lines.extend([
                f"## Background Result: {event.get('task_id', '')}",
                "",
                f"Status: `{event.get('status', '')}`",
                "",
                f"Injected at: `{event.get('injection', '')}`",
                "",
                (f"Output size: `{event.get('original_size', 0)}` chars; "
                 f"truncated: `{truncation}`"),
                "",
                "Command:",
                "",
                _format_markdown_value(event.get("command", "")),
                "",
                "Output:",
                "",
                _format_markdown_value(event.get("summary", "")),
                "",
            ])
        elif event_type == "error":
            lines.extend([
                "## Error",
                "",
                f"Type: `{event.get('error_type', '')}`",
                "",
                _format_markdown_value(event.get("message", "")),
                "",
            ])
        elif event_type == "final_answer":
            lines.extend([
                "## Final Answer",
                "",
                _format_markdown_value(event.get("content", "")),
                "",
            ])
    return "\n".join(lines)


def _run_index_path(workdir: Path) -> Path:
    return Path(workdir) / ".codepilot" / "run_index.json"


def _relative_display_path(path: Path, workdir: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(workdir).resolve()))
    except Exception:
        return str(path)


def _safe_load_run_index(path: Path) -> list[dict]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception:
        try:
            backup = path.with_name(
                f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            path.replace(backup)
        except Exception:
            pass
    return []


def load_run_index(workdir: Path | None = None) -> list[dict]:
    base = Path(workdir) if workdir is not None else Path.cwd()
    return _safe_load_run_index(_run_index_path(base))


def _write_run_index(workdir: Path, items: list[dict]):
    path = _run_index_path(workdir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        tmp_path.write_text(
            json.dumps(items, indent=2, ensure_ascii=False, default=_json_default),
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except Exception:
        try:
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _index_item_from_metadata(run_dir: Path, workdir: Path) -> dict | None:
    try:
        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return None
    except Exception:
        return None
    run_id = metadata.get("run_id") or run_dir.name
    return {
        "run_id": run_id,
        "status": metadata.get("status", "running"),
        "start_time": metadata.get("start_time"),
        "end_time": metadata.get("end_time"),
        "duration_ms": metadata.get("duration_ms"),
        "prompt_preview": metadata.get("prompt_preview", ""),
        "model_provider": metadata.get("model_provider", ""),
        "model": metadata.get("model", ""),
        "workdir": metadata.get("workdir", str(workdir)),
        "tool_count": metadata.get("tool_count", 0),
        "error_count": metadata.get("error_count", 0),
        "blocked_count": metadata.get("blocked_count", 0),
        "event_count": metadata.get("event_count", 0),
        "timeline_event_count": metadata.get("timeline_event_count", 0),
        "pinned": (run_dir / ".keep").exists(),
        "run_dir": _relative_display_path(run_dir, workdir),
        "trace_path": _relative_display_path(run_dir / "trace.jsonl", workdir),
        "timeline_path": _relative_display_path(run_dir / "timeline.jsonl", workdir),
        "timeline_md_path": _relative_display_path(run_dir / "timeline.md", workdir),
        "final_path": _relative_display_path(run_dir / "final.md", workdir),
    }


def _reconcile_run_index(workdir: Path, items: list[dict] | None = None) -> list[dict]:
    items = list(items) if items is not None else load_run_index(workdir)
    runs_dir = Path(workdir) / ".codepilot" / "runs"
    existing = {}
    for info in _scan_runs(runs_dir):
        existing[info["id"]] = info["path"]

    reconciled = []
    seen = set()
    for item in items:
        run_id = item.get("run_id")
        run_dir = existing.get(run_id)
        if not run_id or not run_dir or run_id in seen:
            continue
        fresh = _index_item_from_metadata(run_dir, workdir)
        if fresh:
            item.update(fresh)
        else:
            item["pinned"] = (run_dir / ".keep").exists()
        reconciled.append(item)
        seen.add(run_id)

    for run_id, run_dir in existing.items():
        if run_id in seen:
            continue
        fresh = _index_item_from_metadata(run_dir, workdir)
        if fresh:
            reconciled.append(fresh)

    reconciled.sort(key=lambda item: item.get("start_time") or 0, reverse=True)
    return reconciled


def reconcile_run_index(workdir: Path | None = None) -> list[dict]:
    base = Path(workdir) if workdir is not None else Path.cwd()
    items = _reconcile_run_index(base)
    _write_run_index(base, items)
    return items


def _update_run_index_item(workdir: Path, item: dict):
    try:
        items = load_run_index(workdir)
        run_id = item.get("run_id")
        updated = False
        for index, existing in enumerate(items):
            if existing.get("run_id") == run_id:
                items[index] = item
                updated = True
                break
        if not updated:
            items.append(item)
        items = _reconcile_run_index(workdir, items)
        _write_run_index(workdir, items)
    except Exception:
        return


def list_recent_runs(limit: int = 20, workdir: Path | None = None) -> list[dict]:
    items = load_run_index(workdir)
    return sorted(items, key=lambda item: item.get("start_time") or 0,
                  reverse=True)[:limit]


def list_runs_by_status(status: str, workdir: Path | None = None) -> list[dict]:
    return [item for item in load_run_index(workdir) if item.get("status") == status]


def get_run_summary(run_id: str, workdir: Path | None = None) -> dict | None:
    for item in load_run_index(workdir):
        if item.get("run_id") == run_id:
            return item
    return None


def _mb_to_bytes(value: float) -> int:
    return max(0, int(float(value) * 1024 * 1024))


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except Exception:
        return None


def _is_junction(path: Path) -> bool:
    try:
        checker = getattr(path, "is_junction", None)
        return bool(checker and checker())
    except Exception:
        return False


def _is_safe_run_dir(path: Path, runs_dir: Path) -> bool:
    try:
        if path.parent.resolve() != runs_dir.resolve():
            return False
        if not path.is_dir() or path.is_symlink() or _is_junction(path):
            return False
        return True
    except Exception:
        return False


def _dir_size(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += _dir_size(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except Exception:
                    continue
    except Exception:
        return total
    return total


def _run_start_time(run_dir: Path) -> float:
    metadata_path = run_dir / "metadata.json"
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        start_time = data.get("start_time")
        if isinstance(start_time, (int, float)):
            return float(start_time)
    except Exception:
        pass
    try:
        return run_dir.stat().st_mtime
    except Exception:
        return 0.0


def _run_info(run_dir: Path, current_run_id: str | None = None) -> dict:
    return {
        "id": run_dir.name,
        "path": run_dir,
        "start_time": _run_start_time(run_dir),
        "size": _dir_size(run_dir),
        "pinned": (run_dir / ".keep").exists(),
        "current": bool(current_run_id and run_dir.name == current_run_id),
    }


def _scan_runs(runs_dir: Path, current_run_id: str | None = None) -> list[dict]:
    try:
        entries = list(runs_dir.iterdir())
    except Exception:
        return []
    runs = []
    for entry in entries:
        if not _is_safe_run_dir(entry, runs_dir):
            continue
        runs.append(_run_info(entry, current_run_id))
    return runs


def _is_protected_run(info: dict) -> bool:
    if info.get("current"):
        return True
    return bool(TRACE_KEEP_PINNED and info.get("pinned"))


def _remove_run(info: dict, runs_dir: Path) -> bool:
    run_dir = info["path"]
    if not _is_safe_run_dir(run_dir, runs_dir):
        return False
    try:
        shutil.rmtree(run_dir)
        return True
    except Exception:
        return False


def _safe_remove_dir(path: Path, parent: Path):
    try:
        if path.parent.resolve() != parent.resolve():
            return
        if path.is_dir() and not path.is_symlink() and not _is_junction(path):
            shutil.rmtree(path)
    except Exception:
        return


def _truncate_full_trace(run_dir: Path):
    trace_path = run_dir / "trace.jsonl"
    try:
        if trace_path.exists() and (trace_path.is_symlink() or not trace_path.is_file()):
            return
    except Exception:
        return
    event = {
        "type": "cleanup",
        "ts": time.time(),
        "message": "Full trace truncated because this run exceeded TRACE_MAX_RUN_MB.",
    }
    try:
        trace_path.write_text(
            json.dumps(event, ensure_ascii=False, default=_json_default) + "\n",
            encoding="utf-8",
        )
    except Exception:
        return


def _reduce_large_run(info: dict):
    run_dir = info["path"]
    artifacts = run_dir / "artifacts"
    _safe_remove_dir(artifacts, run_dir)
    if _dir_size(run_dir) > _mb_to_bytes(TRACE_MAX_RUN_MB):
        _truncate_full_trace(run_dir)


def cleanup_old_runs(workdir: Path | None = None, current_run_id: str | None = None) -> dict:
    stats = {
        "deleted": 0,
        "reduced": 0,
        "run_count": 0,
        "total_bytes": 0,
    }
    if not TRACE_CLEANUP_ENABLED:
        return stats
    try:
        base = Path(workdir) if workdir is not None else Path.cwd()
        runs_dir = base / ".codepilot" / "runs"
        if not runs_dir.exists() or not runs_dir.is_dir():
            return stats
        runs_dir_resolved = _safe_resolve(runs_dir)
        if not runs_dir_resolved:
            return stats

        now = time.time()
        cutoff = now - (float(TRACE_RETENTION_MAX_DAYS) * 24 * 60 * 60)
        max_runs = max(0, int(TRACE_RETENTION_MAX_RUNS))
        quota_bytes = _mb_to_bytes(TRACE_RETENTION_MAX_MB)
        single_run_bytes = _mb_to_bytes(TRACE_MAX_RUN_MB)

        runs = _scan_runs(runs_dir, current_run_id)
        stats["run_count"] = len(runs)
        stats["total_bytes"] = sum(info["size"] for info in runs)

        for info in list(runs):
            if _is_protected_run(info):
                continue
            if info["start_time"] < cutoff and _remove_run(info, runs_dir):
                stats["deleted"] += 1

        runs = sorted(_scan_runs(runs_dir, current_run_id),
                      key=lambda item: item["start_time"], reverse=True)
        for index, info in enumerate(runs):
            if index < max_runs or _is_protected_run(info):
                continue
            if _remove_run(info, runs_dir):
                stats["deleted"] += 1

        runs = sorted(_scan_runs(runs_dir, current_run_id),
                      key=lambda item: item["start_time"])
        total = sum(info["size"] for info in runs)
        for info in runs:
            if total <= quota_bytes:
                break
            if _is_protected_run(info):
                continue
            if _remove_run(info, runs_dir):
                stats["deleted"] += 1
                total -= info["size"]

        for info in _scan_runs(runs_dir, current_run_id):
            if _is_protected_run(info):
                continue
            if single_run_bytes and info["size"] > single_run_bytes:
                _reduce_large_run(info)
                stats["reduced"] += 1

        final_runs = _scan_runs(runs_dir, current_run_id)
        stats["run_count"] = len(final_runs)
        stats["total_bytes"] = sum(info["size"] for info in final_runs)
        reconcile_run_index(base)
    except Exception:
        return stats
    return stats


def get_trace_storage_stats(workdir: Path | None = None) -> dict:
    try:
        base = Path(workdir) if workdir is not None else Path.cwd()
        runs_dir = base / ".codepilot" / "runs"
        runs = sorted(_scan_runs(runs_dir), key=lambda item: item["start_time"])
        total = sum(info["size"] for info in runs)
        largest = max(runs, key=lambda item: item["size"], default=None)
        oldest = runs[0] if runs else None
        newest = runs[-1] if runs else None
        return {
            "run_count": len(runs),
            "pinned_run_count": sum(1 for info in runs if info["pinned"]),
            "total_mb": round(total / 1024 / 1024, 3),
            "largest_run": largest["id"] if largest else None,
            "largest_run_mb": round(largest["size"] / 1024 / 1024, 3) if largest else 0,
            "oldest_run": oldest["id"] if oldest else None,
            "newest_run": newest["id"] if newest else None,
        }
    except Exception:
        return {
            "run_count": 0,
            "pinned_run_count": 0,
            "total_mb": 0,
            "largest_run": None,
            "largest_run_mb": 0,
            "oldest_run": None,
            "newest_run": None,
        }


class TraceRun:
    def __init__(self, workdir: Path, model_provider: str, model: str,
                 user_prompt: str = "", storage_root: Path | None = None):
        self.run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.workdir = Path(workdir)
        # Eval runs can keep trusted trace data outside the workspace mounted
        # into the Agent container.  Local/interactive runs retain the existing
        # layout by default.
        self.storage_root = Path(storage_root) if storage_root is not None else self.workdir
        self.model_provider = model_provider
        self.model = model
        self.start_time = time.time()
        self.end_time = None
        self.finished = False
        self.status = "running"
        self.prompt_preview = _redact_text(user_prompt or "")[:120]
        self.tool_count = 0
        self.error_count = 0
        self.blocked_count = 0
        self.event_count = 0
        self.timeline_event_count = 0
        self.run_dir = self.storage_root / ".codepilot" / "runs" / self.run_id
        self.trace_path = self.run_dir / "trace.jsonl"
        self.timeline_path = self.run_dir / "timeline.jsonl"
        self.timeline_md_path = self.run_dir / "timeline.md"
        self.metadata_path = self.run_dir / "metadata.json"
        self.final_path = self.run_dir / "final.md"
        self.timeline_events = []
        self._safe(lambda: self.run_dir.mkdir(parents=True, exist_ok=True))
        self._safe(lambda: self.final_path.write_text("", encoding="utf-8"))
        self._safe(lambda: self.timeline_path.write_text("", encoding="utf-8"))
        self._safe(lambda: self.timeline_md_path.write_text(
            _render_timeline_markdown(self.run_id, []), encoding="utf-8"))
        self.write_metadata()

    def _safe(self, fn):
        try:
            return fn()
        except Exception:
            return None

    def write_metadata(self):
        def _write():
            metadata = {
                "run_id": self.run_id,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "status": self.status,
                "duration_ms": self.duration_ms(),
                "prompt_preview": self.prompt_preview,
                "model_provider": self.model_provider,
                "model": self.model,
                "workdir": str(self.workdir),
                "tool_count": self.tool_count,
                "error_count": self.error_count,
                "blocked_count": self.blocked_count,
                "event_count": self.event_count,
                "timeline_event_count": self.timeline_event_count,
                "trace_path": _relative_display_path(self.trace_path, self.storage_root),
                "timeline_path": _relative_display_path(self.timeline_path, self.storage_root),
                "timeline_md_path": _relative_display_path(self.timeline_md_path, self.storage_root),
                "final_path": _relative_display_path(self.final_path, self.storage_root),
                "pinned": self.is_pinned(),
            }
            self.metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self._safe(_write)

    def duration_ms(self):
        end_time = self.end_time if self.end_time is not None else time.time()
        return int(max(0, (end_time - self.start_time) * 1000))

    def is_pinned(self) -> bool:
        return (self.run_dir / ".keep").exists()

    def index_item(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms(),
            "prompt_preview": self.prompt_preview,
            "model_provider": self.model_provider,
            "model": self.model,
            "workdir": str(self.workdir),
            "tool_count": self.tool_count,
            "error_count": self.error_count,
            "blocked_count": self.blocked_count,
            "event_count": self.event_count,
            "timeline_event_count": self.timeline_event_count,
            "pinned": self.is_pinned(),
            "run_dir": _relative_display_path(self.run_dir, self.storage_root),
            "trace_path": _relative_display_path(self.trace_path, self.storage_root),
            "timeline_path": _relative_display_path(self.timeline_path, self.storage_root),
            "timeline_md_path": _relative_display_path(self.timeline_md_path, self.storage_root),
            "final_path": _relative_display_path(self.final_path, self.storage_root),
        }

    def sync_metadata_and_index(self):
        self.write_metadata()
        _update_run_index_item(self.storage_root, self.index_item())

    def _update_counts(self, event: dict):
        event_type = event.get("type")
        if event_type == "tool_use":
            self.tool_count += 1
        elif event_type == "error":
            self.error_count += 1
        elif (event_type == "hook"
              and event.get("name") == "PreToolUse"
              and event.get("decision") == "blocked"
              and not event.get("recoverable")):
            self.blocked_count += 1

    def event(self, event_type: str, **payload):
        event = {"type": event_type, "ts": time.time(), **_redact(payload)}
        self.event_count += 1
        self._update_counts(event)
        self._safe(lambda: self._append_jsonl(self.trace_path, event))
        timeline_event = _timeline_from_trace_event(event)
        if timeline_event:
            self.timeline_event(timeline_event)
        self.sync_metadata_and_index()

    def _append_jsonl(self, path: Path, event: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=_json_default) + "\n")

    def timeline_event(self, event: dict):
        event = _redact(event)
        self.timeline_events.append(event)
        self.timeline_event_count += 1
        self._safe(lambda: self._append_jsonl(self.timeline_path, event))
        self._safe(lambda: self.timeline_md_path.write_text(
            _render_timeline_markdown(self.run_id, self.timeline_events),
            encoding="utf-8"))

    def infer_final_status(self, final_answer: str = "") -> str:
        text = str(final_answer or "").strip().lower()
        if self.blocked_count:
            return "blocked"
        if text.startswith("permission denied"):
            return "blocked"
        if "timeoutexpired" in text or text.startswith("error: timeout") or "[timeout" in text:
            return "timeout"
        if self.error_count or text.startswith("[error]") or text.startswith("error:"):
            return "failed"
        return "success"

    def finish(self, final_answer: str = "", status: str | None = None):
        if self.finished:
            return
        self.finished = True
        self.status = status if status in RUN_STATUSES else self.infer_final_status(final_answer)
        self.end_time = time.time()
        self._safe(lambda: self.final_path.write_text(_redact_text(final_answer or ""), encoding="utf-8"))
        self.sync_metadata_and_index()


def start_run(user_prompt: str, *, workdir: Path, model_provider: str, model: str,
              storage_root: Path | None = None) -> TraceRun:
    global CURRENT_TRACE
    CURRENT_TRACE = TraceRun(workdir, model_provider, model, user_prompt,
                             storage_root=storage_root)
    CURRENT_TRACE.event("user_prompt", prompt=user_prompt)
    cleanup_old_runs(workdir=CURRENT_TRACE.storage_root,
                     current_run_id=CURRENT_TRACE.run_id)
    return CURRENT_TRACE


def get_current_run():
    return CURRENT_TRACE


def record_event(event_type: str, **payload):
    run = get_current_run()
    if run and not getattr(run, "finished", False):
        run.event(event_type, **payload)


def record_hook(name: str, **payload):
    record_event("hook", name=name, **payload)


def record_llm_request(*, model: str, max_tokens: int, message_count: int,
                       tool_count: int, purpose: str = "lead",
                       agent_role: str = ""):
    record_event("llm_request", model=model, max_tokens=max_tokens,
                 message_count=message_count, tool_count=tool_count,
                 purpose=purpose, agent_role=agent_role)


def record_llm_response(response, *, purpose: str = "lead", agent_role: str = ""):
    record_event("llm_response", stop_reason=getattr(response, "stop_reason", None),
                 content=_truncate(_content_text(getattr(response, "content", ""))),
                 purpose=purpose, agent_role=agent_role)


def record_tool_use(block):
    record_event("tool_use", tool=getattr(block, "name", ""),
                 tool_use_id=getattr(block, "id", ""),
                 input=getattr(block, "input", {}))


def record_tool_result(tool_use_id: str, tool: str, result):
    record_event("tool_result", tool=tool, tool_use_id=tool_use_id,
                 content=_truncate(result))


def record_error(error):
    record_event("error", error_type=type(error).__name__, message=str(error))


def finish_run(final_answer: str = "", status: str | None = None):
    global CURRENT_TRACE
    run = get_current_run()
    if not run:
        return
    if getattr(run, "finished", False):
        CURRENT_TRACE = None
        return
    record_event("final_answer", content=final_answer or "")
    run.finish(final_answer or "", status=status)
    CURRENT_TRACE = None
