"""Host-side agents read only their repository: the planner and the team lead.

They run on the host with ``read_file`` and ``glob``. The Aqours file tools
already keep what they return inside the workspace: ``read_file`` resolves the
path (following symlinks) and refuses one outside it, and ``glob`` drops
matches outside it. But ``glob`` with an absolute pattern ignores its root and
walks the host (``C:/Users/<user>/**/site-packages/...``), and both accept an
absolute path or a ``..`` that stays inside. Planning dask 2023.8.0 spent
glob calls looking for an installed dask that way.

:class:`RepoGuard` is a PreToolUse hook registered by ``worker_entry`` for
these agents only (``WorkerRequest.repo_guard``): a path or pattern that is
absolute, contains ``..``, or resolves (after symlinks) outside the
workspace is refused before the tool runs, with a short message. Workers
(graph nodes, teammates, single agents) do not get it.
"""
from __future__ import annotations

import glob
from pathlib import Path, PurePosixPath, PureWindowsPath

REFUSAL = ("Tool not run: you may only read files of the repository. Give a path or "
           "pattern relative to the repository root, without '..'.")

# the argument each guarded tool reads
GUARDED_FIELDS = {"read_file": "path", "glob": "pattern"}


def _is_absolute(text: str) -> bool:
    slashed = text.replace("\\", "/")
    windows = PureWindowsPath(text)
    return (slashed.startswith(("/", "~")) or bool(windows.drive) or windows.is_absolute()
            or PurePosixPath(slashed).is_absolute())


def _literal_prefix(pattern: str) -> str:
    """The leading components of a glob pattern that have no wildcard."""
    parts = []
    for part in pattern.split("/"):
        if glob.has_magic(part):
            break
        parts.append(part)
    return "/".join(parts)


def outside_reason(value: str, root: Path, *, pattern: bool = False) -> str | None:
    """Why ``value`` is not a path (or glob pattern) inside ``root``; None when it is.

    ``"absolute"``, ``"parent"`` (a ``..`` component) or ``"outside"`` (it, or
    a pattern's part before the first wildcard, resolves outside ``root``,
    for example through a symlink). An empty value is left to the tool.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if _is_absolute(text):
        return "absolute"
    slashed = text.replace("\\", "/")
    if ".." in slashed.split("/"):
        return "parent"
    target = _literal_prefix(slashed) if pattern else slashed
    if target:
        base = root.resolve()
        try:
            resolved = (base / target).resolve()
        except (OSError, RuntimeError):  # a symlink loop, an invalid name
            return "outside"
        if not resolved.is_relative_to(base):
            return "outside"
    return None


class RepoGuard:
    """Refuse ``read_file`` and ``glob`` arguments outside the workspace."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.active = True  # off after the run, so in-process runs do not leak it
        self.refused: list[dict] = []

    def check(self, tool: str, args: dict) -> str | None:
        """The refusal reason for one tool call, or None."""
        field = GUARDED_FIELDS.get(tool)
        if field is None:
            return None
        return outside_reason(str((args or {}).get(field, "")), self.workspace,
                              pattern=tool == "glob")

    def pre_tool(self, block, reject):
        """The PreToolUse hook: ``reject(message)`` for a refused call, else None."""
        if not self.active:
            return None
        why = self.check(block.name, block.input)
        if why is None:
            return None
        self.refused.append({"tool": block.name,
                             "value": str(block.input.get(GUARDED_FIELDS[block.name], "")),
                             "why": why})
        return reject(REFUSAL)
