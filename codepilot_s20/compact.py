from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

from .model_budget import can_spend_optional_calls
from .runtime import AgentRuntime
from .runtime_state import *


# A compacted conversation has only two durable pieces: one cumulative Markdown
# checkpoint and a small verbatim suffix. Current runtime facts are rebuilt by
# context.py and never inferred from this checkpoint.
CONTEXT_CHECKPOINT_MARKER = "[Context checkpoint]"
COMPACT_TRIGGER_RATIO = 0.85
RECENT_TAIL_MAX_TOKENS = 8_000
RECENT_TAIL_RATIO = 0.15
COMPACT_OUTPUT_RESERVE_CHARS = 6_000
SUMMARY_MAX_TOKENS = 2_000
SUMMARY_OUTPUT_RESERVE_CHARS = SUMMARY_MAX_TOKENS * 3
ARCHIVE_PREVIEW_CHARS = 2_000
ARCHIVE_URI_PREFIX = "archive://"
CONTEXT_ARCHIVE_ACTIVE_TTL_SECONDS = 24 * 60 * 60
CONTEXT_ARCHIVE_ACTIVE_MARKER = ".active"
_ARCHIVE_LOCK = threading.RLock()
_LEGACY_CONTEXT_SESSION_ID = "session-legacy-" + uuid.uuid4().hex

COMPACTION_PROMPT = """\
You are creating a context checkpoint for another coding-agent model
that will continue the current task.

Summarize the older conversation history into a concise continuation handoff.
The newest messages may remain available verbatim outside this summary, so
focus on older information that is still needed to continue correctly.

Preserve:
- the user's current goal and hard constraints
- progress already made and the current focus
- important decisions and why they were made
- concrete information learned from important tool results
- relevant files, file responsibilities, symbols, code behavior and relationships
- changes already performed
- commands and tests run, including their outcomes
- exact error messages and failed approaches when still relevant
- unresolved problems and immediate next steps

For tool results, preserve what was learned from them. Do not merely state
that a command was executed or that a file was read.
If a tool result is represented by an archived-result descriptor, its exact
content remains recoverable through that descriptor. Preserve the tool_use_id
when an exact detail may matter later. Do not copy an archive manifest into
the checkpoint.

Use exact file paths, symbol names, commands, errors, URLs and identifiers
when they matter. Give more detail to recent and currently relevant work.
Remove obsolete, duplicated, conversational and low-value details.

If an earlier context checkpoint is present, update it: preserve facts that
are still true, remove stale facts, and merge in newer information.

Do not answer the original task. Do not describe the summarization process.
Do not invent facts. Return concise Markdown only.
"""


class ContextCompactionError(RuntimeError):
    """Raised when a successful summary still cannot fit the request budget."""


def estimate_size(messages: list) -> int:
    return len(json.dumps(
        messages,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def estimate_context_size(
    messages: list,
    *,
    system: str = "",
    tools: list | None = None,
    dynamic: dict | None = None,
) -> int:
    """Conservatively estimate every assembled request component."""
    return len(json.dumps(
        {
            "system": system,
            "messages": messages,
            "tools": tools or [],
            "dynamic": dynamic or {},
        },
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def estimate_context_tokens(size_chars: int) -> int:
    # The core supports multiple providers and has no common tokenizer. Three
    # characters per token is deliberately conservative for mixed code/CJK.
    return (max(0, int(size_chars)) + 2) // 3


def block_type(block):
    return (
        block.get("type")
        if isinstance(block, dict)
        else getattr(block, "type", None)
    )


def _block_field(block, name: str, default=None):
    return (
        block.get(name, default)
        if isinstance(block, dict)
        else getattr(block, name, default)
    )


def message_has_tool_use(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "assistant"
        and isinstance(content, list)
        and any(block_type(block) == "tool_use" for block in content)
    )


def is_tool_result_message(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and any(block_type(block) == "tool_result" for block in content)
    )


def collect_tool_results(messages: list):
    found = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if block_type(block) == "tool_result":
                found.append((message_index, block_index, block))
    return found


def _tool_use_ids(message: dict) -> set[str]:
    if not message_has_tool_use(message):
        return set()
    return {
        str(_block_field(block, "id", ""))
        for block in message["content"]
        if block_type(block) == "tool_use"
        and _block_field(block, "id", "")
    }


def _tool_result_ids(message: dict) -> set[str]:
    if not is_tool_result_message(message):
        return set()
    return {
        str(_block_field(block, "tool_use_id", ""))
        for block in message["content"]
        if block_type(block) == "tool_result"
        and _block_field(block, "tool_use_id", "")
    }


def _history_units(messages: list) -> list[list[dict]]:
    """Group messages at boundaries that never split a tool exchange."""
    units: list[list[dict]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message_has_tool_use(message) and index + 1 < len(messages):
            following = messages[index + 1]
            use_ids = _tool_use_ids(message)
            result_ids = _tool_result_ids(following)
            if result_ids and result_ids <= use_ids:
                units.append([message, following])
                index += 2
                continue
        units.append([message])
        index += 1
    return units


def _is_checkpoint_message(message: dict) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return content.lstrip().startswith(CONTEXT_CHECKPOINT_MARKER)
    if isinstance(content, list):
        first_text = next(
            (
                str(_block_field(block, "text", ""))
                for block in content
                if block_type(block) == "text"
            ),
            "",
        )
        return first_text.lstrip().startswith(CONTEXT_CHECKPOINT_MARKER)
    return False


def _is_user_instruction(message: dict) -> bool:
    if (
        message.get("role") != "user"
        or is_tool_result_message(message)
        or _is_checkpoint_message(message)
    ):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return bool(str(content).strip())
    return any(
        block_type(block) == "text"
        and str(_block_field(block, "text", "")).strip()
        for block in content
    )


def _flatten(units: list[list[dict]]) -> list[dict]:
    return [message for unit in units for message in unit]


def _recent_tail_budget_chars(usable_context_chars: int) -> int:
    usable_tokens = estimate_context_tokens(usable_context_chars)
    tail_tokens = min(
        RECENT_TAIL_MAX_TOKENS,
        max(1, int(usable_tokens * RECENT_TAIL_RATIO)),
    )
    return tail_tokens * 3


def _select_prefix_and_recent_tail(
    messages: list,
    *,
    tail_budget_chars: int,
) -> tuple[list[dict], list[dict]]:
    """Select a contiguous recent suffix while keeping tool pairs atomic."""
    units = _history_units(messages)
    if not units:
        return [], []

    start = len(units)
    used = 0
    for index in range(len(units) - 1, -1, -1):
        unit = units[index]
        # Prior checkpoints must be folded into the next checkpoint rather than
        # accumulating in the raw tail.
        if any(_is_checkpoint_message(message) for message in unit):
            break
        unit_size = estimate_size(unit)
        if start < len(units) and used + unit_size > tail_budget_chars:
            break
        start = index
        used += unit_size

    if start == len(units):
        start = len(units) - 1

    prefix_units = units[:start]
    tail_units = units[start:]
    tail = deepcopy(_flatten(tail_units))

    # Preserve the latest human instruction verbatim even when a long tool run
    # pushed it outside the bounded suffix. The original copy remains in the
    # summarized prefix, so chronological evidence is not rewritten.
    tail_message_ids = {id(message) for unit in tail_units for message in unit}
    latest_user = next(
        (
            message
            for message in reversed(messages)
            if _is_user_instruction(message)
        ),
        None,
    )
    if latest_user is not None and id(latest_user) not in tail_message_ids:
        tail.insert(0, deepcopy(latest_user))

    return deepcopy(_flatten(prefix_units)), tail


def _collect_tool_uses(messages: list) -> dict[str, dict]:
    uses = {}
    for message in messages:
        if not message_has_tool_use(message):
            continue
        for block in message["content"]:
            if block_type(block) != "tool_use":
                continue
            tool_use_id = str(_block_field(block, "id", ""))
            if not tool_use_id:
                continue
            tool_input = _block_field(block, "input", {})
            uses[tool_use_id] = {
                "tool_name": str(_block_field(block, "name", "")),
                "tool_input": (
                    deepcopy(tool_input)
                    if isinstance(tool_input, dict)
                    else {}
                ),
            }
    return uses


def _context_session_id(runtime: AgentRuntime | None) -> str:
    if runtime is None:
        return _LEGACY_CONTEXT_SESSION_ID
    value = str(runtime.state.metadata.get("context_session_id", ""))
    if not re.fullmatch(r"session-[A-Za-z0-9_-]{8,100}", value):
        value = "session-" + uuid.uuid4().hex
        runtime.state.metadata["context_session_id"] = value
    return value


def _archive_location(
    runtime: AgentRuntime | None,
    *,
    create: bool,
) -> tuple[str, Path]:
    session_id = _context_session_id(runtime)
    archive_base = (
        runtime.paths.context_archive_root
        if runtime is not None
        else Path(WORKDIR)
    )
    archive_parent = (
        runtime.paths.context_archive_dir
        if runtime is not None
        else archive_base / ".codepilot" / "context-archives"
    )
    root = archive_parent / session_id
    if (
        archive_parent.is_symlink()
        or not archive_parent.resolve().is_relative_to(
            Path(archive_base).resolve()
        )
    ):
        raise OSError("context archive root cannot be a symlink")
    if create:
        archive_parent.mkdir(parents=True, exist_ok=True)
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise OSError("context session archive is not a safe directory")
        root.mkdir(exist_ok=True)
        _mark_context_archive_active(root, session_id)
        results_dir = root / "results"
        if results_dir.exists() and (
            results_dir.is_symlink() or not results_dir.is_dir()
        ):
            raise OSError("context archive results is not a safe directory")
        results_dir.mkdir(exist_ok=True)
        metadata_path = root / "metadata.json"
        if metadata_path.is_symlink():
            raise OSError("context archive metadata cannot be a symlink")
        _touch_archive_metadata(root, session_id)
    elif (
        root.is_dir()
        and runtime is not None
        and runtime.state.metadata.get("context_archive_active", True)
    ):
        try:
            _mark_context_archive_active(root, session_id)
        except OSError:
            pass
    return session_id, root


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _mark_context_archive_active(
    archive_root: Path,
    session_id: str,
) -> None:
    marker = archive_root / CONTEXT_ARCHIVE_ACTIVE_MARKER
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise OSError("context archive active marker is not a safe file")
    if marker.exists():
        os.utime(marker, None)
        return
    raw = json.dumps({
        "context_session_id": session_id,
        "pid": os.getpid(),
        "activated_at": time.time(),
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(marker, raw)


def refresh_context_archive_session(runtime: AgentRuntime) -> None:
    """Best-effort heartbeat for an active Runtime's existing archive."""
    if not runtime.state.metadata.get("context_archive_active", True):
        return
    try:
        _archive_location(runtime, create=False)
    except OSError:
        return


def release_context_archive_session(runtime: AgentRuntime) -> None:
    """Release one Runtime's active marker without deleting its archive."""
    runtime.state.metadata["context_archive_active"] = False
    try:
        _, root = _archive_location(runtime, create=False)
        marker = root / CONTEXT_ARCHIVE_ACTIVE_MARKER
        if marker.is_symlink() or marker.is_file():
            marker.unlink(missing_ok=True)
    except OSError:
        return


def _touch_archive_metadata(
    archive_root: Path,
    session_id: str,
    *,
    timestamp: float | None = None,
) -> None:
    metadata_path = archive_root / "metadata.json"
    now = time.time() if timestamp is None else float(timestamp)
    created_at = now
    try:
        current = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(current, dict):
            created_at = float(current.get("created_at", now))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    raw = json.dumps({
        "context_session_id": session_id,
        "created_at": created_at,
        "updated_at": now,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(metadata_path, raw)


def _read_manifest(manifest_path: Path) -> list[dict]:
    records = []
    if manifest_path.is_symlink():
        return records
    try:
        lines = manifest_path.read_bytes().splitlines()
    except OSError:
        return records
    invalid = 0
    for raw_line in lines:
        if not raw_line.strip():
            continue
        try:
            line = raw_line.decode("utf-8", errors="strict")
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError, TypeError):
            invalid += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            invalid += 1
    if invalid:
        try:
            record_event(
                "archive_warning",
                reason="invalid_manifest_records",
                invalid_record_count=invalid,
                manifest=str(manifest_path),
            )
        except Exception:
            pass
    return records


def _record_path(archive_root: Path, record: dict) -> Path | None:
    filename = str(record.get("filename", ""))
    relative = Path(filename)
    if (
        not filename
        or relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) != 2
        or relative.parts[0] != "results"
    ):
        return None
    try:
        results_dir = archive_root / "results"
        candidate = archive_root / relative
        if (
            archive_root.is_symlink()
            or results_dir.is_symlink()
            or candidate.is_symlink()
        ):
            return None
        resolved = candidate.resolve()
        results_root = results_dir.resolve()
        if resolved.parent != results_root:
            return None
        return resolved
    except OSError:
        return None


def _record_is_reusable(
    archive_root: Path,
    record: dict,
    digest: str,
) -> bool:
    output_path = _record_path(archive_root, record)
    if output_path is None:
        return False
    try:
        return (
            output_path.is_file()
            and not output_path.is_symlink()
            and hashlib.sha256(output_path.read_bytes()).hexdigest() == digest
        )
    except OSError:
        return False


def _archive_uri(session_id: str) -> str:
    return (
        f"{ARCHIVE_URI_PREFIX}context/{session_id}/manifest.jsonl"
    )


def _write_manifest_atomic(
    manifest_path: Path,
    records: list[dict],
) -> None:
    raw = "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        for record in records
    ).encode("utf-8")
    _atomic_write_bytes(manifest_path, raw)


def _archive_prefix_tool_results(
    prefix: list,
    runtime: AgentRuntime | None,
) -> tuple[list[dict], str]:
    """Archive every exact Tool Result that is about to leave live context."""
    session_id, archive_root = _archive_location(runtime, create=True)
    manifest_path = archive_root / "manifest.jsonl"
    results = collect_tool_results(prefix)
    if not results:
        return [], _archive_uri(session_id) if manifest_path.exists() else ""
    tool_uses = _collect_tool_uses(prefix)
    archived = []
    created_paths: list[Path] = []
    with _ARCHIVE_LOCK:
        existing = _read_manifest(manifest_path)
        by_key = {
            (str(item.get("tool_use_id", "")), str(item.get("sha256", ""))):
            item
            for item in existing
            if str(item.get("context_session_id", "")) == session_id
        }
        appended = []
        try:
            for _, _, block in results:
                tool_use_id = str(_block_field(
                    block, "tool_use_id", "unknown",
                ))
                output = str(_block_field(block, "content", ""))
                raw = output.encode("utf-8")
                digest = hashlib.sha256(raw).hexdigest()
                existing_record = by_key.get((tool_use_id, digest))
                if (
                    existing_record is not None
                    and _record_is_reusable(
                        archive_root, existing_record, digest,
                    )
                ):
                    archived.append(existing_record)
                    continue

                archive_id = (
                    f"ar_{digest[:12]}_{uuid.uuid4().hex[:10]}"
                )
                relative_filename = f"results/{archive_id}.txt"
                output_path = archive_root / Path(relative_filename)
                _atomic_write_bytes(output_path, raw)
                created_paths.append(output_path)
                use = tool_uses.get(tool_use_id, {})
                record = {
                    "archive_id": archive_id,
                    "context_session_id": session_id,
                    "tool_use_id": tool_use_id,
                    "tool_name": str(use.get("tool_name", "")),
                    "tool_input": dict(use.get("tool_input", {})),
                    "character_count": len(output),
                    "sha256": digest,
                    "filename": relative_filename,
                    "archived_at": time.time(),
                }
                by_key[(tool_use_id, digest)] = record
                appended.append(record)
                archived.append(record)
            if appended:
                _touch_archive_metadata(archive_root, session_id)
                _write_manifest_atomic(
                    manifest_path,
                    [*existing, *appended],
                )
        except Exception:
            for created_path in created_paths:
                try:
                    created_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
    return archived, _archive_uri(session_id)


def _tool_input_preview(value, limit: int = 500):
    try:
        rendered = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"),
        )
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= limit:
        return value if isinstance(value, dict) else {"value": rendered}
    return {"preview": rendered[:limit], "truncated": True}


def search_archived_tool_results(
    query: str = "",
    tool_name: str = "",
    limit: int = 20,
    runtime: AgentRuntime | None = None,
) -> str:
    """Search bounded metadata from the current Context Session manifest."""
    try:
        session_id, archive_root = _archive_location(runtime, create=False)
        records = [
            record
            for record in _read_manifest(archive_root / "manifest.jsonl")
            if str(record.get("context_session_id", "")) == session_id
        ]
        needle = str(query or "").casefold()
        tool_filter = str(tool_name or "").casefold()
        matches = []
        for record in reversed(records):
            if (
                tool_filter
                and str(record.get("tool_name", "")).casefold()
                != tool_filter
            ):
                continue
            haystack = "\n".join((
                str(record.get("archive_id", "")),
                str(record.get("tool_use_id", "")),
                str(record.get("tool_name", "")),
                json.dumps(
                    record.get("tool_input", {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            )).casefold()
            if needle and needle not in haystack:
                continue
            matches.append({
                "archive_id": str(record.get("archive_id", "")),
                "tool_use_id": str(record.get("tool_use_id", "")),
                "tool_name": str(record.get("tool_name", "")),
                "tool_input": _tool_input_preview(
                    record.get("tool_input", {}),
                ),
                "character_count": int(
                    record.get("character_count", 0) or 0,
                ),
                "sha256": str(record.get("sha256", "")),
                "archived_at": record.get("archived_at"),
            })
            if len(matches) >= min(20, max(1, int(limit or 20))):
                break
        return json.dumps(
            matches,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception as exc:
        return json.dumps({
            "error": f"{type(exc).__name__}: {exc}",
            "results": [],
        }, ensure_ascii=False, separators=(",", ":"))


def read_archived_tool_result(
    archive_id: str = "",
    tool_use_id: str = "",
    offset: int | None = None,
    limit: int | None = None,
    runtime: AgentRuntime | None = None,
) -> str:
    """Read one exact result from the current Context Session archive."""
    if not str(archive_id or tool_use_id).strip():
        return "Error: archive_id or tool_use_id is required"
    try:
        session_id, archive_root = _archive_location(runtime, create=False)
        records = [
            record
            for record in _read_manifest(archive_root / "manifest.jsonl")
            if str(record.get("context_session_id", "")) == session_id
        ]
        selected_by_tool = False
        if archive_id:
            record = next(
                (
                    item for item in records
                    if str(item.get("archive_id", "")) == str(archive_id)
                ),
                None,
            )
        else:
            record = next(
                (
                    item for item in reversed(records)
                    if str(item.get("tool_use_id", "")) == str(tool_use_id)
                ),
                None,
            )
            selected_by_tool = record is not None
        requested = archive_id or tool_use_id
        if record is None:
            return f"Error: archived tool result not found: {requested}"
        output_path = _record_path(archive_root, record)
        if (
            output_path is None
            or not output_path.is_file()
            or output_path.is_symlink()
        ):
            return "Error: invalid archived tool result path"
        raw = output_path.read_bytes()
        expected_digest = str(record.get("sha256", ""))
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            return "Error: archived tool result digest mismatch"
        output = raw.decode("utf-8")
        if offset is None and limit is None:
            selected = output
        else:
            lines = output.splitlines()
            start = max(0, int(offset or 0))
            selected_lines = lines[start:]
            if limit is not None:
                selected_lines = selected_lines[:max(0, int(limit))]
            selected = "\n".join(selected_lines)
        if selected_by_tool:
            return (
                f"[Resolved latest archive_id="
                f"{record.get('archive_id', '')} for tool_use_id="
                f"{tool_use_id}]\n{selected}"
            )
        return selected
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def _archive_dir_size(path: Path) -> int:
    total = 0
    try:
        for candidate in path.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
    except OSError:
        pass
    return total


def _archive_updated_at(path: Path) -> float:
    try:
        metadata = json.loads(
            (path / "metadata.json").read_text(encoding="utf-8"),
        )
        return float(metadata.get("updated_at", path.stat().st_mtime))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def cleanup_context_archives(
    current_session_id: str,
    *,
    context_archive_dir: str | Path,
    max_age_days: float | None = None,
    max_sessions: int | None = None,
    max_total_mb: float | None = None,
    active_ttl_seconds: float | None = None,
) -> dict:
    """Best-effort whole-session retention for Context archives."""
    stats = {"deleted": 0, "session_count": 0, "total_bytes": 0}
    try:
        archive_parent = Path(context_archive_dir)
        if not archive_parent.is_dir():
            return stats
        archive_base = archive_parent.parent.parent
        if (
            archive_parent.is_symlink()
            or not archive_parent.resolve().is_relative_to(
                archive_base.resolve()
            )
        ):
            return stats
        age_days = (
            TRACE_RETENTION_MAX_DAYS
            if max_age_days is None else float(max_age_days)
        )
        session_limit = (
            TRACE_RETENTION_MAX_RUNS
            if max_sessions is None else int(max_sessions)
        )
        quota_bytes = int(max(
            0.0,
            TRACE_RETENTION_MAX_MB
            if max_total_mb is None else float(max_total_mb),
        ) * 1024 * 1024)
        active_ttl = max(
            0.0,
            CONTEXT_ARCHIVE_ACTIVE_TTL_SECONDS
            if active_ttl_seconds is None else float(active_ttl_seconds),
        )
        now = time.time()

        def active_marker_is_fresh(path: Path) -> bool:
            marker = path / CONTEXT_ARCHIVE_ACTIVE_MARKER
            try:
                if marker.is_symlink() or not marker.is_file():
                    return False
                if now - marker.stat().st_mtime <= active_ttl:
                    return True
                marker.unlink(missing_ok=True)
            except OSError:
                return False
            return False

        def scan():
            sessions = []
            for path in archive_parent.iterdir():
                try:
                    if (
                        not path.is_dir()
                        or path.is_symlink()
                        or path.parent.resolve() != archive_parent.resolve()
                    ):
                        continue
                    active = active_marker_is_fresh(path)
                    sessions.append({
                        "id": path.name,
                        "path": path,
                        "updated_at": _archive_updated_at(path),
                        "size": _archive_dir_size(path),
                        "active": active,
                        "protected": (
                            path.name == current_session_id
                            or (path / ".keep").exists()
                            or active
                        ),
                    })
                except OSError:
                    continue
            return sessions

        def remove(info):
            if info["protected"]:
                return False
            try:
                shutil.rmtree(info["path"])
                stats["deleted"] += 1
                return True
            except OSError:
                return False

        cutoff = time.time() - max(0.0, age_days) * 86400
        for info in scan():
            if info["updated_at"] < cutoff:
                remove(info)

        sessions = sorted(
            scan(), key=lambda item: item["updated_at"], reverse=True,
        )
        for index, info in enumerate(sessions):
            if index >= max(0, session_limit):
                remove(info)

        sessions = sorted(scan(), key=lambda item: item["updated_at"])
        total = sum(info["size"] for info in sessions)
        for info in sessions:
            if total <= quota_bytes:
                break
            if remove(info):
                total -= info["size"]
        sessions = scan()
        stats["session_count"] = len(sessions)
        stats["total_bytes"] = sum(info["size"] for info in sessions)
    except Exception:
        return stats
    return stats


def initialize_context_archive(runtime: AgentRuntime) -> None:
    """Run archive retention once for an already-created Context Session."""
    if runtime.state.metadata.get("context_archive_initialized"):
        return
    runtime.state.metadata["context_archive_initialized"] = True
    try:
        session_id = _context_session_id(runtime)
        cleanup_context_archives(
            session_id,
            context_archive_dir=runtime.paths.context_archive_dir,
        )
    except Exception:
        return


def _archived_descriptor(record: dict, output: str) -> str:
    preview_chars = min(ARCHIVE_PREVIEW_CHARS, max(0, len(output)))
    head_chars = preview_chars // 2
    tail_chars = preview_chars - head_chars
    head = output[:head_chars]
    tail = output[-tail_chars:] if len(output) > preview_chars else ""
    return "\n".join([
        "<archived-tool-result>",
        f"tool_use_id: {record.get('tool_use_id', '')}",
        f"tool_name: {record.get('tool_name', '')}",
        "tool_input: " + json.dumps(
            record.get("tool_input", {}),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        f"archive_id: {record.get('archive_id', '')}",
        f"filename: {record.get('filename', '')}",
        f"character_count: {record.get('character_count', len(output))}",
        f"sha256: {record.get('sha256', '')}",
        "First output:",
        head,
        "Last output:",
        tail,
        "</archived-tool-result>",
    ])


def write_transcript(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> Path:
    transcript_dir = (
        runtime.paths.transcript_dir
        if runtime is not None
        else TRANSCRIPT_DIR
    )
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{time.time_ns()}.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for message in messages:
            stream.write(json.dumps(
                message,
                default=str,
                ensure_ascii=False,
            ) + "\n")
    return path


def _compact_prompt(prefix: list) -> str:
    return (
        COMPACTION_PROMPT
        + "\n\nOlder conversation history to summarize:\n"
        + json.dumps(
            prefix,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _call_compact_model(
    prompt: str,
    *,
    runtime: AgentRuntime | None,
    purpose: str,
) -> str:
    model = runtime.config.model if runtime is not None else MODEL
    model_client = (
        runtime.services.model_client if runtime is not None else client
    )
    record_llm_request(
        model=model,
        max_tokens=SUMMARY_MAX_TOKENS,
        message_count=1,
        tool_count=0,
        purpose=purpose,
        agent_role="",
    )
    response = model_client.messages.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    record_llm_response(response, purpose=purpose, agent_role="")
    return extract_text(response.content)


def summarize_history(
    messages: list,
    runtime: AgentRuntime | None = None,
) -> str:
    """Return a plain Markdown continuation checkpoint."""
    return _call_compact_model(
        _compact_prompt(messages),
        runtime=runtime,
        purpose="compact_summary",
    ).strip()


def _prepare_summary_input(
    prefix: list,
    archived: list[dict],
    *,
    max_prompt_chars: int,
) -> tuple[list | None, int]:
    """Fit one summary request by masking archived result copies only."""
    summary_input = deepcopy(prefix)
    if len(_compact_prompt(summary_input)) <= max_prompt_chars:
        return summary_input, 0
    records = {
        (str(record.get("tool_use_id", "")), str(record.get("sha256", ""))):
        record
        for record in archived
    }
    candidates = []
    for _, _, block in collect_tool_results(summary_input):
        output = str(_block_field(block, "content", ""))
        tool_use_id = str(_block_field(block, "tool_use_id", ""))
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        record = records.get((tool_use_id, digest))
        if record is not None:
            candidates.append((len(output), block, output, record))
    masked = 0
    for _, block, output, record in sorted(candidates, reverse=True, key=lambda x: x[0]):
        descriptor = _archived_descriptor(record, output)
        if isinstance(block, dict):
            block["content"] = descriptor
        else:
            setattr(block, "content", descriptor)
        masked += 1
        if len(_compact_prompt(summary_input)) <= max_prompt_chars:
            return summary_input, masked
    return None, masked


def _summarize_once(
    summary_input: list,
    runtime: AgentRuntime | None,
) -> tuple[str | None, str]:
    try:
        summary = (
            summarize_history(summary_input, runtime)
            if runtime is not None
            else summarize_history(summary_input)
        )
    except Exception as exc:
        record_event(
            "compact_summary_error",
            attempt=1,
            error_type=type(exc).__name__,
            error=str(exc)[:1000],
        )
        return None, f"{type(exc).__name__}: {exc}"
    if not str(summary or "").strip():
        return None, "summary model returned empty text"
    return str(summary).strip(), ""


def _strip_archive_locator(summary: str) -> str:
    return re.sub(
        r"(?ms)\n*^## Archived tool results\s*$.*?(?=^##\s|\Z)",
        "",
        summary,
    ).strip()


def _append_archive_locator(summary: str, manifest_uri: str) -> str:
    summary = _strip_archive_locator(summary)
    if not manifest_uri:
        return summary
    return (
        summary
        + "\n\n## Archived tool results\n\n"
        + f"Archive session: `{manifest_uri}`\n\n"
        + "When an exact old tool result is needed, first use "
        + "`search_archived_tool_results`, then call "
        + "`read_archived_tool_result(archive_id=...)`. Do not rerun an "
        + "unchanged tool only to recover output that is already archived."
    )


def _checkpoint_message(summary: str) -> dict:
    return {
        "role": "user",
        "content": f"{CONTEXT_CHECKPOINT_MARKER}\n{summary.strip()}",
    }


def _merge_user_content(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left + "\n\n" + right
    left_blocks = (
        [{"type": "text", "text": left}]
        if isinstance(left, str)
        else deepcopy(left) if isinstance(left, list)
        else [{"type": "text", "text": str(left)}]
    )
    right_blocks = (
        [{"type": "text", "text": right}]
        if isinstance(right, str)
        else deepcopy(right) if isinstance(right, list)
        else [{"type": "text", "text": str(right)}]
    )
    return left_blocks + right_blocks


def _assemble_compacted_history(summary: str, tail: list) -> list:
    candidate = [_checkpoint_message(summary), *deepcopy(tail)]
    # Only the checkpoint boundary is synthetic. Merge it with an adjacent user
    # message so providers that require alternating roles receive valid history.
    if len(candidate) >= 2 and candidate[1].get("role") == "user":
        candidate[0]["content"] = _merge_user_content(
            candidate[0]["content"],
            candidate[1].get("content", ""),
        )
        del candidate[1]
    return candidate


def _request_sizer(
    request_size_fn,
    *,
    system: str,
    tools: list | None,
):
    return request_size_fn or (
        lambda candidate: estimate_context_size(
            candidate,
            system=system,
            tools=tools,
        )
    )


def _record_compact(
    *,
    reason: str,
    transcript: Path,
    before_messages: int,
    before_size: int,
    after_messages: int,
    after_size: int,
    summarized_prefix: int,
    tail: list,
    summary: str,
    success: bool,
    failure_reason: str = "",
    archived_results: int = 0,
    masked_summary_results: int = 0,
    summary_model_calls: int = 0,
    archive_failed: bool = False,
    archive_error_type: str = "",
) -> None:
    try:
        record_event(
            "compact",
            kind=reason,
            reason=reason,
            transcript=str(transcript),
            before_messages=before_messages,
            before_size=before_size,
            before_tokens=estimate_context_tokens(before_size),
            after_messages=after_messages,
            after_size=after_size,
            after_tokens=estimate_context_tokens(after_size),
            summarized_prefix_messages=summarized_prefix,
            recent_tail_messages=len(tail),
            recent_tail_tokens=estimate_context_tokens(estimate_size(tail)),
            summary_length=len(summary),
            success=success,
            failure_reason=failure_reason,
            oversized_result_handled=masked_summary_results > 0,
            archived_result_count=archived_results,
            masked_summary_result_count=masked_summary_results,
            summary_model_calls=summary_model_calls,
            archive_failed=archive_failed,
            archive_error_type=archive_error_type,
        )
    except Exception:
        pass


def _compact(
    messages: list,
    *,
    reason: str,
    runtime: AgentRuntime | None,
    target_context_budget: int | None,
    request_size_fn,
    system: str,
    tools: list | None,
    force: bool,
    allow_model_summary: bool | None,
) -> list:
    transcript = write_transcript(messages, runtime)
    sizer = _request_sizer(request_size_fn, system=system, tools=tools)
    before_size = sizer(messages)
    target = max(
        1_000,
        int(
            target_context_budget
            if target_context_budget is not None
            else CONTEXT_LIMIT - COMPACT_OUTPUT_RESERVE_CHARS
        ),
    )
    trigger = int(CONTEXT_LIMIT * COMPACT_TRIGGER_RATIO)

    if not force and before_size <= trigger:
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=0,
            tail=messages,
            summary="",
            success=False,
            failure_reason="below compact trigger",
        )
        return messages

    model_client = runtime.services.model_client if runtime is not None else client
    budget_allowed, budget = can_spend_optional_calls(model_client, 1)
    if allow_model_summary is False or not budget_allowed:
        record_event(
            "model_budget_guard",
            decision="compact_skipped",
            reason="finalization_reserve",
            estimated_calls=1,
            **{
                key: value
                for key, value in budget.items()
                if key != "available"
            },
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=0,
            tail=messages,
            summary="",
            success=False,
            failure_reason="model summary call unavailable",
        )
        return messages

    fixed_request_size = sizer([])
    usable_message_chars = target - fixed_request_size
    locator_reserve = 800
    tail_capacity = (
        usable_message_chars
        - SUMMARY_OUTPUT_RESERVE_CHARS
        - locator_reserve
    )
    if tail_capacity < 256:
        failure = (
            "fixed request plus summary output reserve leaves no safe recent "
            "tail budget"
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=0,
            tail=messages,
            summary="",
            success=False,
            failure_reason=failure,
        )
        return messages
    tail_budget = min(
        _recent_tail_budget_chars(target),
        tail_capacity,
    )
    prefix, tail = _select_prefix_and_recent_tail(
        messages,
        tail_budget_chars=tail_budget,
    )
    if not prefix:
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=0,
            tail=messages,
            summary="",
            success=False,
            failure_reason="no safe older prefix to summarize",
        )
        return messages

    reserved_candidate = _assemble_compacted_history(
        "x" * (SUMMARY_OUTPUT_RESERVE_CHARS + locator_reserve),
        tail,
    )
    if sizer(reserved_candidate) > target:
        failure = (
            "complete recent tail cannot fit with the reserved checkpoint; "
            "tail Tool Results remain verbatim"
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary="",
            success=False,
            failure_reason=failure,
        )
        return messages

    try:
        archived, manifest_uri = _archive_prefix_tool_results(prefix, runtime)
    except Exception as exc:
        failure = f"archive failed: {type(exc).__name__}: {exc}"
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary="",
            success=False,
            failure_reason=failure,
            summary_model_calls=0,
            archive_failed=True,
            archive_error_type=type(exc).__name__,
        )
        return messages
    summary_prompt_budget = max(
        1_000,
        CONTEXT_LIMIT - SUMMARY_OUTPUT_RESERVE_CHARS,
    )
    summary_input, masked_count = _prepare_summary_input(
        prefix,
        archived,
        max_prompt_chars=summary_prompt_budget,
    )
    if summary_input is None:
        failure = (
            "safe summary request cannot fit without deleting pinned "
            "checkpoint or user instructions"
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary="",
            success=False,
            failure_reason=failure,
            archived_results=len(archived),
            masked_summary_results=masked_count,
        )
        return messages

    summary, failure = _summarize_once(summary_input, runtime)
    if not summary:
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary="",
            success=False,
            failure_reason=failure,
            archived_results=len(archived),
            masked_summary_results=masked_count,
            summary_model_calls=1,
        )
        return messages

    checkpoint = _append_archive_locator(summary, manifest_uri)
    candidate = _assemble_compacted_history(checkpoint, tail)
    candidate_size = sizer(candidate)
    if candidate_size > target:
        failure = (
            f"assembled request remains {candidate_size} chars above target "
            f"{target}; a second summary call is forbidden"
        )
        _record_compact(
            reason=reason,
            transcript=transcript,
            before_messages=len(messages),
            before_size=before_size,
            after_messages=len(messages),
            after_size=before_size,
            summarized_prefix=len(prefix),
            tail=tail,
            summary=checkpoint,
            success=False,
            failure_reason=failure,
            archived_results=len(archived),
            masked_summary_results=masked_count,
            summary_model_calls=1,
        )
        return messages

    _record_compact(
        reason=reason,
        transcript=transcript,
        before_messages=len(messages),
        before_size=before_size,
        after_messages=len(candidate),
        after_size=candidate_size,
        summarized_prefix=len(prefix),
        tail=tail,
        summary=checkpoint,
        success=True,
        archived_results=len(archived),
        masked_summary_results=masked_count,
        summary_model_calls=1,
    )
    if runtime is not None:
        runtime.state.metadata["compact_generation"] = (
            int(runtime.state.metadata.get("compact_generation", 0)) + 1
        )
    return candidate


def compact_history(
    messages: list,
    *,
    allow_model_summary: bool | None = None,
    reason: str = "automatic",
    runtime: AgentRuntime | None = None,
    target_context_budget: int | None = None,
    request_size_fn=None,
    system: str = "",
    tools: list | None = None,
) -> list:
    return _compact(
        messages,
        reason=reason or "automatic",
        runtime=runtime,
        target_context_budget=target_context_budget,
        request_size_fn=request_size_fn,
        system=system,
        tools=tools,
        force=(reason == "manual"),
        allow_model_summary=allow_model_summary,
    )


def reactive_compact(
    messages: list,
    runtime: AgentRuntime | None = None,
    *,
    target_context_budget: int | None = None,
    request_size_fn=None,
    system: str = "",
    tools: list | None = None,
) -> list:
    return _compact(
        messages,
        reason="reactive",
        runtime=runtime,
        target_context_budget=target_context_budget,
        request_size_fn=request_size_fn,
        system=system,
        tools=tools,
        force=True,
        allow_model_summary=None,
    )


import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
