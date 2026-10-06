"""Context-use metrics from worker traces (file-tool calls, not inferred shell IO)."""
from __future__ import annotations

import json
import posixpath
from pathlib import Path

from .escapes import attempt_traces


def relative_file(path: str, workspace: str | None) -> str:
    """Normalize relative, host-worktree and Docker /workspace paths equally."""
    path = posixpath.normpath(path.replace("\\", "/"))
    roots = ["/workspace"]
    if workspace:
        roots.insert(0, workspace.replace("\\", "/").rstrip("/"))
    for root in roots:
        if path.startswith(root + "/"):
            return path[len(root) + 1:]
    return path


def trace_metrics(paths: list[Path], full_files: set[str], own_files: set[str], *,
                  workspace: str | None = None, first_write_seen: bool = False) -> dict:
    """Count read_file and llm_request events, stopping the latter at a file write.

    The request producing the first write_file/edit_file is included. No-write
    attempts count all model requests. Pass first_write_seen between attempts
    so retries do not restart a node's pre-write count. Tool results are not
    counted a second time. Arbitrary bash commands are not interpreted as IO.
    """
    reads = calls = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # a timeout can leave a partial final record
            if not isinstance(event, dict):
                continue
            if event.get("type") == "llm_request" and not first_write_seen:
                calls += 1
            if event.get("type") != "tool_use":
                continue
            tool = event.get("tool")
            if tool in {"write_file", "edit_file"}:
                first_write_seen = True
            data = event.get("input")
            if tool == "read_file" and isinstance(data, dict) and isinstance(data.get("path"), str):
                file = relative_file(data["path"], workspace)
                if file not in own_files and file not in full_files:
                    reads += 1
    return {"reads_outside_pack": reads, "calls_before_first_write": calls,
            "first_write_seen": first_write_seen}


def node_context_metrics(node_dir: Path, attempts: list[dict]) -> dict:
    """Combine each attempt's trace with the exact pack provided to that attempt."""
    total = {"reads_outside_pack": 0, "calls_before_first_write": 0}
    written = False
    for metadata in attempts:
        metrics = trace_metrics(
            attempt_traces(node_dir, metadata["attempt"]),
            set(metadata["full_files"]), set(metadata["own_files"]),
            workspace=metadata["workspace"], first_write_seen=written)
        for key in total:
            total[key] += metrics[key]
        written = metrics["first_write_seen"]
    return total
