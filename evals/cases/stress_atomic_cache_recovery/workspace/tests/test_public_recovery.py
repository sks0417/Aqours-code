from __future__ import annotations

from artifact_cache import ArtifactCache
from conftest import request


def test_recovery_keeps_live_writer_and_removes_it_after_expiry(tmp_path, clock):
    cache = ArtifactCache(tmp_path / "cache", clock=clock, lease_seconds=10)
    lease = cache.begin_build(request(), writer_id="worker")
    (lease.staging_dir / "partial.bin").write_bytes(b"partial")

    live = cache.recover()
    assert live.kept_active_builds == 1
    assert live.removed_staging_dirs == 0
    assert lease.staging_dir.is_dir()

    clock.set(lease.expires_at)
    expired = cache.recover()
    assert expired.removed_staging_dirs == 1
    assert not lease.staging_dir.exists()
    repeated = cache.recover()
    assert repeated.removed_staging_dirs == 0


def test_recovery_preserves_unrelated_files(cache):
    note = cache.root / "operator-note.txt"
    note.write_text("do not delete", encoding="utf-8")

    cache.recover()

    assert note.read_text(encoding="utf-8") == "do not delete"
