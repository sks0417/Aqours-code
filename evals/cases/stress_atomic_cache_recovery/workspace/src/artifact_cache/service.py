from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

from .key import cache_key
from .lock import LeaseRegistry
from .models import (
    BuildLease,
    BuildRequest,
    CacheEntry,
    InvalidRequest,
    RecoveryReport,
    StaleWriter,
)
from .recovery import RecoveryManager
from .store import CacheStore


class ArtifactCache:
    def __init__(self, root: str | Path, *, clock: Callable[[], float] | None = None,
                 lease_seconds: float = 30.0,
                 fault_hook: Callable[[str, Path], None] | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.monotonic
        self._fault_hook = fault_hook
        self._store = CacheStore(self.root)
        self._leases = LeaseRegistry(self.root, lease_seconds=lease_seconds)
        self._recovery = RecoveryManager(self._store, self._leases)

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidRequest("clock must return a finite number")
        value = float(value)
        if not math.isfinite(value):
            raise InvalidRequest("clock must return a finite number")
        return value

    def key_for(self, request: BuildRequest) -> str:
        return cache_key(request)

    def get(self, request: BuildRequest) -> CacheEntry | None:
        return self._store.read(self.key_for(request))

    def _begin_locked(self, request: BuildRequest, writer_id: str) -> BuildLease:
        if not isinstance(writer_id, str) or not writer_id.strip():
            raise InvalidRequest("writer_id must be a non-empty string")
        if not isinstance(request.artifact_format, str) or not request.artifact_format:
            raise InvalidRequest("artifact_format must be a non-empty string")
        lease = self._leases.acquire(
            self.key_for(request), writer_id.strip(), request.artifact_format,
            now=self._now(),
        )
        self._store.create_staging(lease)
        return lease

    def begin_build(self, request: BuildRequest, *, writer_id: str) -> BuildLease:
        key = self.key_for(request)
        with self._leases.mutex(key):
            return self._begin_locked(request, writer_id)

    def commit(self, lease: BuildLease, artifact: object) -> CacheEntry:
        if not isinstance(lease, BuildLease):
            raise StaleWriter("lease must be a BuildLease")
        with self._leases.mutex(lease.cache_key):
            already = self._store.committed_for_lease(lease, artifact)
            if already is not None:
                return already
            self._leases.validate(lease, now=self._now())
            entry = self._store.publish(
                lease,
                artifact,
                created_at=self._now(),
                fault_hook=self._fault_hook,
                validate=lambda: self._leases.validate(lease, now=self._now()),
            )
            self._leases.finish(lease, state="committed", now=self._now())
            return entry

    def abort(self, lease: BuildLease) -> None:
        if not isinstance(lease, BuildLease):
            raise StaleWriter("lease must be a BuildLease")
        with self._leases.mutex(lease.cache_key):
            self._leases.abort(lease, now=self._now())
            self._store.cleanup_staging(lease)

    def get_or_build(self, request: BuildRequest, builder: Callable[[Path], object],
                     *, writer_id: str) -> CacheEntry:
        if not callable(builder):
            raise InvalidRequest("builder must be callable")
        key = self.key_for(request)
        with self._leases.mutex(key):
            existing = self._store.read(key)
            if existing is not None:
                return existing
            lease = self._begin_locked(request, writer_id)
            artifact = builder(lease.staging_dir)
            return self.commit(lease, artifact)

    def recover(self) -> RecoveryReport:
        return self._recovery.recover(now=self._now())
