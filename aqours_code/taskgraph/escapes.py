"""Find workers that tried to reach files outside their workspace.

A worker may only use its own worktree. ``escape_reasons`` judges one tool
call; ``scan_trace`` applies it to an Aqours trace file; ``scan_run`` and
``audit_runs`` apply it to every node attempt of a run directory (used after
each run and by ``python -m aqours_code.taskgraph audit``).

An escape attempt is any of:

- a file tool (``read_file``, ``write_file``, ``edit_file``, ``glob``) whose
  path is a host absolute path or leaves the workspace through ``..``, or
  whose call was refused with "Path escapes workspace";
- a ``bash`` command that names a host absolute path (``C:\\``, ``D:/``,
  ``\\\\server\\``, ``/c/``), ``/mnt/``, ``/home/``, ``/Users/``, or leaves the
  workspace (``cd ..``, ``../`` outside the workspace, ``~``);
- file content (``write_file``, ``edit_file``) that names a host absolute
  path, ``/mnt/``, ``/home/`` or ``/Users/`` (a script that reads elsewhere).

Absolute paths inside the node's own workspace are allowed, and so is
``/workspace``, where the Docker sandbox mounts it.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

FILE_TOOLS = {"read_file": "path", "write_file": "path", "edit_file": "path",
              "glob": "pattern"}
CONTENT_FIELDS = {"write_file": ("content",), "edit_file": ("new_text",)}
REFUSAL = "path escapes workspace"
SAMPLE_LIMIT = 5
SAMPLE_CHARS = 200

_DRIVE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
_UNC = re.compile(r"(?:^|(?<=[\s\"'=(]))\\\\[A-Za-z0-9._$-]{2,}\\[A-Za-z0-9._$-]")
_HOST_DIRS = re.compile(r"(?<![\w.~-])/(?:mnt|home|Users)/")
_DRIVE_MOUNT = re.compile(r"(?<![\w.~:-])/[a-zA-Z]/[\w.-]")
_SHELL_SPLIT = re.compile(r"[\s;&|<>()`]+")
_CONTAINER_WORKSPACE = re.compile(r"(?<![\w.-])/workspace(?=[/\\s\"']|$)")


@dataclass
class EscapeAttempt:
    """One tool call that tried to leave the workspace."""

    tool: str
    detail: str
    reasons: list[str] = field(default_factory=list)
    attempt: int | None = None

    def to_dict(self) -> dict:
        """JSON form, as stored in summary.json."""
        return asdict(self)


def _without_workspace(text: str, workspace: str | None) -> str:
    """Replace every spelling of ``workspace`` (and ``/workspace``) with ``.``."""
    text = _CONTAINER_WORKSPACE.sub(".", text)
    if not workspace:
        return text
    spellings = {workspace, workspace.replace("\\", "/"), workspace.replace("/", "\\")}
    for spelling in sorted(spellings, key=len, reverse=True):
        spelling = spelling.rstrip("\\/")
        if spelling:
            text = re.sub(re.escape(spelling), ".", text, flags=re.IGNORECASE)
    return text


def host_path_reasons(text: str, *, shell: bool = False) -> list[str]:
    """Why ``text`` names a location outside the workspace (empty: it does not)."""
    reasons = []
    if _DRIVE.search(text):
        reasons.append("host absolute path (drive)")
    if _UNC.search(text):
        reasons.append("host network path")
    if _HOST_DIRS.search(text):
        reasons.append("host directory (/mnt, /home, /Users)")
    if shell and _DRIVE_MOUNT.search(text):
        reasons.append("host drive mount (/c/...)")
    return reasons


def _leaves_workspace(token: str) -> bool:
    token = token.strip("\"'")
    if token in ("~",) or token.startswith(("~/", "~\\")):
        return True
    normalized = posixpath.normpath(token.replace("\\", "/")) if token else ""
    return normalized == ".." or normalized.startswith("../")


def bash_reasons(command: str) -> list[str]:
    """Why a shell command reaches outside the workspace."""
    reasons = host_path_reasons(command, shell=True)
    tokens = [token for token in _SHELL_SPLIT.split(command) if token]
    if any(_leaves_workspace(token) for token in tokens):
        reasons.append("leaves the workspace (.. or ~)")
    return reasons


def escape_reasons(tool: str, tool_input: Mapping | None,
                   workspace: str | None = None) -> list[str]:
    """Why one tool call tries to reach outside ``workspace`` (empty: it does not)."""
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    reasons: list[str] = []
    if tool == "bash":
        command = _without_workspace(str(tool_input.get("command", "")), workspace)
        reasons += bash_reasons(command)
    if tool in FILE_TOOLS:
        path = _without_workspace(str(tool_input.get(FILE_TOOLS[tool], "")), workspace)
        reasons += [f"{reason} in {FILE_TOOLS[tool]}"
                    for reason in host_path_reasons(path)]
        if path.startswith(("/", "\\")) and not path.startswith(("./", ".\\")):
            reasons.append(f"absolute {FILE_TOOLS[tool]}")
        if _leaves_workspace(path):
            reasons.append(f"{FILE_TOOLS[tool]} leaves the workspace")
    for name in CONTENT_FIELDS.get(tool, ()):
        content = _without_workspace(str(tool_input.get(name, "")), workspace)
        reasons += [f"{reason} in written content" for reason in host_path_reasons(content)]
    return list(dict.fromkeys(reasons))


def is_refusal(tool: str, content: object) -> bool:
    """True if a file tool's result says the path escapes the workspace."""
    return tool in FILE_TOOLS and REFUSAL in str(content).lower()


def _detail(tool: str, tool_input: Mapping | None) -> str:
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    key = "command" if tool == "bash" else FILE_TOOLS.get(tool)
    text = str(tool_input.get(key, "")) if key else json.dumps(tool_input)[:SAMPLE_CHARS]
    text = " ".join(text.split())
    return text if len(text) <= SAMPLE_CHARS else text[:SAMPLE_CHARS - 3] + "..."


def scan_trace(path: Path, workspace: str | None = None,
               attempt: int | None = None) -> list[EscapeAttempt]:
    """Every escape attempt in one Aqours trace file, in trace order."""
    found: dict[str, EscapeAttempt] = {}
    order: list[EscapeAttempt] = []
    calls: dict[str, tuple[str, Mapping]] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for number, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        tool = str(record.get("tool", ""))
        call_id = str(record.get("tool_use_id") or f"line-{number}")
        if record.get("type") == "tool_use":
            tool_input = record.get("input") or {}
            calls[call_id] = (tool, tool_input)
            reasons = escape_reasons(tool, tool_input, workspace)
            if reasons:
                found[call_id] = EscapeAttempt(tool, _detail(tool, tool_input), reasons,
                                               attempt)
                order.append(found[call_id])
        elif record.get("type") == "tool_result" and is_refusal(tool, record.get("content")):
            requested = calls.get(call_id, (tool, {}))[1]
            if _without_workspace(str(requested.get(FILE_TOOLS[tool], "") if isinstance(
                    requested, Mapping) else ""), None).startswith("./"):
                continue  # /workspace/...: the container path, refused by a host tool
            if call_id in found:
                if "refused by the file tool" not in found[call_id].reasons:
                    found[call_id].reasons.append("refused by the file tool")
                continue
            _, tool_input = calls.get(call_id, (tool, {}))
            found[call_id] = EscapeAttempt(tool, _detail(tool, tool_input),
                                           ["refused by the file tool"], attempt)
            order.append(found[call_id])
    return order


@dataclass
class NodeEscapes:
    """Escape attempts of every attempt of one node."""

    attempts: list[EscapeAttempt] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Number of escape attempts."""
        return len(self.attempts)

    def samples(self, limit: int = SAMPLE_LIMIT) -> list[dict]:
        """The first ``limit`` attempts as dicts."""
        return [attempt.to_dict() for attempt in self.attempts[:limit]]


_TRACE_NAME = re.compile(r"^trace_(\d+)\.jsonl$")


def scan_node(node_dir: Path) -> NodeEscapes:
    """Scan ``trace_<n>.jsonl`` of every attempt in a node log directory."""
    result = NodeEscapes()
    traces = []
    for trace in node_dir.glob("trace_*.jsonl"):
        match = _TRACE_NAME.match(trace.name)
        if match:
            traces.append((int(match.group(1)), trace))
    for attempt, trace in sorted(traces):
        workspace = None
        try:
            config = json.loads((node_dir / f"worker_{attempt}_config.json")
                                .read_text(encoding="utf-8"))
            workspace = config.get("workspace")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        result.attempts += scan_trace(trace, workspace, attempt)
    return result


def scan_run(run_dir: Path) -> dict[str, NodeEscapes]:
    """Escape attempts per node of one run directory (``nodes/<id>/``)."""
    nodes_dir = run_dir / "nodes"
    if not nodes_dir.is_dir():
        return {}
    return {node_dir.name: scan_node(node_dir)
            for node_dir in sorted(nodes_dir.iterdir()) if node_dir.is_dir()}


def is_run_dir(path: Path) -> bool:
    """True for a Coordinator run directory."""
    return (path / "nodes").is_dir() and ((path / "config.json").is_file()
                                          or (path / "graph.json").is_file())


def run_dirs(path: Path) -> list[Path]:
    """``path`` itself if it is a run directory, else its run subdirectories."""
    if is_run_dir(path):
        return [path]
    if not path.is_dir():
        return []
    return [child for child in sorted(path.iterdir()) if is_run_dir(child)]


def graph_name(run_dir: Path) -> str:
    """The run's request id, from config.json or graph.json."""
    for name, key in (("config.json", "request_id"), ("graph.json", "request_id")):
        try:
            value = json.loads((run_dir / name).read_text(encoding="utf-8")).get(key)
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if value:
            return str(value)
    return "?"


@dataclass
class AuditRow:
    """One line of the audit table."""

    run_id: str
    graph: str
    node: str
    escapes: int
    sample: str


def audit_runs(paths: Iterable[Path]) -> list[AuditRow]:
    """One row per node of every run directory found under ``paths``."""
    rows = []
    for path in paths:
        for run_dir in run_dirs(Path(path)):
            name = graph_name(run_dir)
            for node, escapes in scan_run(run_dir).items():
                sample = ""
                if escapes.attempts:
                    first = escapes.attempts[0]
                    sample = f"{first.tool}: {first.detail}"
                rows.append(AuditRow(run_dir.name, name, node, escapes.count, sample))
    return rows


def format_audit(rows: list[AuditRow], sample_chars: int = 80) -> str:
    """A plain-text table of ``rows`` plus a total line."""
    header = ("run", "graph", "node", "escapes", "sample")
    table = [header] + [(row.run_id, row.graph, row.node, str(row.escapes),
                         row.sample[:sample_chars]) for row in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(header) - 1)]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(line[:-1], widths))
             + "  " + line[-1] for line in table]
    runs = len({row.run_id for row in rows})
    total = sum(row.escapes for row in rows)
    dirty = len({row.run_id for row in rows if row.escapes})
    lines.append(f"runs: {runs}  nodes: {len(rows)}  escape attempts: {total}  "
                 f"runs with escapes: {dirty}")
    return "\n".join(line.rstrip() for line in lines)
