r"""Delete what a Windows host cannot handle by its plain path.

A worker's shell runs in a Linux container. A Windows habit such as
``dir /b src 2>nul`` creates a file named ``nul`` there. On a Windows host
that name is a device: git cannot add the file, and deleting it by its plain
path fails. The ``\\?\`` form of a path names the file itself.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

WINDOWS = os.name == "nt"
RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{number}" for number in range(1, 10)]
    + [f"LPT{number}" for number in range(1, 10)])


def is_reserved_name(name: str) -> bool:
    """True if ``name`` without its extension is a Windows device name."""
    return name.split(".", 1)[0].rstrip(" ").upper() in RESERVED_NAMES


def extended_path(path: str | os.PathLike[str]) -> str:
    r"""The absolute ``\\?\`` form of ``path`` on Windows; ``path`` elsewhere."""
    text = os.fspath(path)
    if not WINDOWS or text.startswith("\\\\?\\"):
        return text
    # abspath turns a last component named nul into the device \\.\nul, so
    # only the directory goes through it.
    parent, name = os.path.split(text.replace("/", "\\"))
    text = os.path.join(os.path.abspath(parent), name)
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def remove_reserved_files(root: Path) -> list[str]:
    """Delete every file under ``root`` with a reserved name; return their paths.

    The paths are relative to ``root``, with forward slashes, and sorted.
    ``.git`` is not searched.
    """
    removed = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            if not is_reserved_name(name):
                continue
            target = extended_path(os.path.join(dirpath, name))
            try:
                os.unlink(target)
            except PermissionError:  # read-only on Windows
                os.chmod(target, stat.S_IWRITE)
                os.unlink(target)
            removed.append((Path(dirpath).relative_to(root) / name).as_posix())
    return sorted(removed)


def _retry_delete(func, path, _exc_info) -> None:
    r"""rmtree error handler: clear the read-only bit, then try ``\\?\`` on Windows."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        if not WINDOWS:
            raise
        literal = extended_path(path)
        os.chmod(literal, stat.S_IWRITE)
        func(literal)


def remove_tree(path: Path) -> None:
    """Delete ``path`` if it exists, including read-only git files on Windows."""
    if path.exists():
        shutil.rmtree(path, onerror=_retry_delete)
