from __future__ import annotations

import json
import shutil
from pathlib import Path

from .lock import LeaseRegistry
from .models import RecoveryReport
from .store import CacheStore


class RecoveryManager:
    def __init__(self, store: CacheStore, leases: LeaseRegistry) -> None:
        self.store = store
        self.leases = leases

    def recover(self, *, now: float) -> RecoveryReport:
        removed_staging = 0
        removed_keys: set[str] = set()
        for path in sorted(self.store.staging.iterdir()):
            if not path.is_dir():
                continue
            marker_path = path / "lease.json"
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                cache_key = str(marker.get("cache_key") or "")
            except (OSError, json.JSONDecodeError):
                cache_key = ""
            shutil.rmtree(path)
            removed_staging += 1
            if cache_key:
                removed_keys.add(cache_key)
                self.leases.abandon_if_expired(cache_key, now=now)

        kept_entries = 0
        for entry_root in sorted(self.store.entries.iterdir()):
            if entry_root.is_dir() and (entry_root / "current.json").is_file():
                kept_entries += 1
        return RecoveryReport(
            kept_entries=kept_entries,
            removed_staging_dirs=removed_staging,
            removed_cache_keys=tuple(sorted(removed_keys)),
        )

