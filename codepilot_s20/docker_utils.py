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
