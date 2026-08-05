from __future__ import annotations

import json
import math
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from .models import BuildInProgress, BuildLease, InvalidRequest, StaleWriter


class LeaseRegistry:
    _registry_guard = threading.Lock()
    _key_mutexes: dict[tuple[str, str], threading.RLock] = {}

    def __init__(self, root: Path, *, lease_seconds: float) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
            raise InvalidRequest("lease_seconds must be a positive number")
        lease_seconds = float(lease_seconds)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise InvalidRequest("lease_seconds must be a positive number")
        self.root = root.resolve()
        self.lock_root = self.root / "locks"
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds

    def mutex(self, cache_key: str) -> threading.RLock:
        identity = (str(self.root), cache_key)
        with self._registry_guard:
            return self._key_mutexes.setdefault(identity, threading.RLock())

    def state_path(self, cache_key: str) -> Path:
        return self.lock_root / f"{cache_key}.json"

    def read(self, cache_key: str) -> dict[str, Any] | None:
        path = self.state_path(cache_key)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, cache_key: str, value: dict[str, Any]) -> None:
        target = self.state_path(cache_key)
        temporary = target.with_suffix(f".json.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)

    def acquire(self, cache_key: str, writer_id: str, artifact_format: str,
                *, now: float) -> BuildLease:
        current = self.read(cache_key) or {}
        if current.get("state") == "active" and float(current.get("expires_at", 0)) > now:
            raise BuildInProgress(cache_key)
        generation = int(current.get("generation", 0)) + 1
        token = f"{cache_key[:8]}-{uuid.uuid4().hex}"
        expires_at = now + self.lease_seconds
        staging_dir = self.root / "staging" / f"{cache_key}.g{generation}.{token}"
        value = {
            "cache_key": cache_key,
            "writer_id": writer_id,
            "generation": generation,
            "token": token,
            "expires_at": expires_at,
            "state": "active",
            "staging_dir": staging_dir.name,
            "artifact_format": artifact_format,
        }
        self._write(cache_key, value)
        return BuildLease(
            cache_key=cache_key,
            writer_id=writer_id,
            generation=generation,
            token=token,
            expires_at=expires_at,
            staging_dir=staging_dir,
            artifact_format=artifact_format,
        )

    def validate(self, lease: BuildLease, *, now: float) -> dict[str, Any]:
        current = self.read(lease.cache_key)
        token_matches_namespace = lease.token.startswith(f"{lease.cache_key[:8]}-")
        if current is None or current.get("state") != "active" or not token_matches_namespace:
            raise StaleWriter(f"writer is no longer current for {lease.cache_key}")
        return current

    def finish(self, lease: BuildLease, *, state: str, now: float) -> None:
        current = self.validate(lease, now=now)
        current["state"] = state
        self._write(lease.cache_key, current)

    def abort(self, lease: BuildLease, *, now: float) -> None:
        try:
            self.finish(lease, state="aborted", now=now)
        except StaleWriter:
            return

    def abandon_if_expired(self, cache_key: str, *, now: float) -> bool:
        current = self.read(cache_key)
        if not current or current.get("state") != "active":
            return False
        if float(current.get("expires_at", 0)) > now:
            return False
        current["state"] = "abandoned"
        self._write(cache_key, current)
        return True

