from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


def normalize_bind_source(source: str | Path, *, platform: str | None = None) -> str:
    """Return a Docker --mount source without interpreting Windows colons."""
    platform = os.name if platform is None else platform
    raw = os.fspath(source)
    if platform == "nt":
        return str(PureWindowsPath(raw))
    return str(Path(raw).resolve())


def host_container_user(
    *,
    platform: str | None = None,
    getuid=None,
    getgid=None,
) -> str:
    """Match POSIX bind-mount ownership while staying non-root."""
    platform = os.name if platform is None else platform
    if platform == "nt":
        return "10001:10001"
    getuid = getuid or getattr(os, "getuid", lambda: 10001)
    getgid = getgid or getattr(os, "getgid", lambda: 10001)
    uid, gid = int(getuid()), int(getgid())
    if uid <= 0:
        # Running the sandbox as root is forbidden. The startup write probe will
        # fail clearly if this fallback identity cannot write the bind mount.
        return "10001:10001"
    if gid <= 0:
        gid = uid
    return f"{uid}:{gid}"


def prepare_disposable_tree(
    root: str | Path,
    *,
    allowed_root: str | Path,
    uid: int = 10001,
    gid: int = 10001,
    platform: str | None = None,
    getuid=None,
    chown=None,
) -> bool:
    """Give a fallback non-root container ownership of one disposable copy.

    This is intentionally a no-op except on POSIX hosts running as UID 0. It
    never traverses symlinks and refuses paths outside the per-case output root.
    """
    platform = os.name if platform is None else platform
    if platform == "nt":
        return False
    getuid = getuid or getattr(os, "getuid", lambda: -1)
    if int(getuid()) != 0:
        return False
    chown = chown or os.chown
    root_path = Path(root)
    allowed_path = Path(allowed_root).resolve()
    if root_path.is_symlink():
        raise ValueError(f"refusing to prepare symlink root: {root_path}")
    resolved_root = root_path.resolve()
    try:
        resolved_root.relative_to(allowed_path)
    except ValueError as exc:
        raise ValueError(f"disposable path escapes case output: {root_path}") from exc

    def visit(path: Path):
        if path.is_dir():
            with os.scandir(path) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        visit(entry_path)
                    else:
                        chown(entry_path, uid, gid, follow_symlinks=False)
        chown(path, uid, gid, follow_symlinks=False)

    visit(resolved_root)
    return True
