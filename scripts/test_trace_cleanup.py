from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from codepilot_s20 import trace  # noqa: E402


class CheckSuite:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"PASS {name}")
            return
        self.failed += 1
        suffix = f" - {detail}" if detail else ""
        print(f"FAIL {name}{suffix}")

    def run(self, name: str, fn):
        try:
            fn(self)
        except Exception as exc:
            self.failed += 1
            print(f"FAIL {name} - unexpected {type(exc).__name__}: {exc}")

    def finish(self) -> int:
        print()
        print(f"Summary: {self.passed} PASS, {self.failed} FAIL")
        return 1 if self.failed else 0


def make_fake_run(base: Path, name: str, *, start_time: float | None = None,
                  pinned: bool = False, missing_metadata: bool = False,
                  bad_metadata: bool = False, trace_bytes: int = 0,
                  artifacts_bytes: int = 0) -> Path:
    run_dir = base / ".codepilot" / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    if bad_metadata:
        (run_dir / "metadata.json").write_text("{bad json", encoding="utf-8")
    elif not missing_metadata:
        metadata = {"run_id": name, "start_time": start_time or time.time()}
        (run_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8")

    (run_dir / "timeline.jsonl").write_text("", encoding="utf-8")
    (run_dir / "timeline.md").write_text("# Timeline\n", encoding="utf-8")
    (run_dir / "final.md").write_text("final", encoding="utf-8")
    (run_dir / "trace.jsonl").write_text("x" * trace_bytes, encoding="utf-8")

    if artifacts_bytes:
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "large.bin").write_text("y" * artifacts_bytes, encoding="utf-8")
    if pinned:
        (run_dir / ".keep").write_text("", encoding="utf-8")
    return run_dir


def patch_retention(**overrides):
    keys = [
        "TRACE_CLEANUP_ENABLED",
        "TRACE_RETENTION_MAX_DAYS",
        "TRACE_RETENTION_MAX_RUNS",
        "TRACE_RETENTION_MAX_MB",
        "TRACE_MAX_RUN_MB",
        "TRACE_KEEP_PINNED",
    ]
    old = {key: getattr(trace, key) for key in keys}
    for key, value in overrides.items():
        setattr(trace, key, value)
    return old


def restore_retention(old):
    for key, value in old.items():
        setattr(trace, key, value)


def test_ttl_and_pin(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_cleanup_ttl_") as tmp:
        root = Path(tmp)
        old = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=1,
            TRACE_RETENTION_MAX_RUNS=100,
            TRACE_RETENTION_MAX_MB=1000,
            TRACE_MAX_RUN_MB=1000,
            TRACE_KEEP_PINNED=True,
        )
        try:
            now = time.time()
            expired = make_fake_run(root, "expired", start_time=now - 3 * 24 * 60 * 60)
            fresh = make_fake_run(root, "fresh", start_time=now)
            pinned = make_fake_run(
                root, "pinned", start_time=now - 3 * 24 * 60 * 60, pinned=True)
            trace.reconcile_run_index(root)

            trace.cleanup_old_runs(workdir=root)

            suite.check("TTL deletes expired unpinned run", not expired.exists())
            suite.check("TTL keeps fresh run", fresh.exists())
            suite.check(".keep pinned run is preserved", pinned.exists())
            index_ids = {item["run_id"] for item in trace.load_run_index(root)}
            suite.check("Deleted run is removed from run_index", "expired" not in index_ids)
            pinned_item = trace.get_run_summary("pinned", root)
            suite.check("Pinned run_index item has pinned=true",
                        bool(pinned_item and pinned_item["pinned"] is True))
        finally:
            restore_retention(old)


def test_sliding_window_and_current_run(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_cleanup_window_") as tmp:
        root = Path(tmp)
        old = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=1000,
            TRACE_RETENTION_MAX_RUNS=1,
            TRACE_RETENTION_MAX_MB=1000,
            TRACE_MAX_RUN_MB=1000,
            TRACE_KEEP_PINNED=True,
        )
        try:
            now = time.time()
            oldest = make_fake_run(root, "oldest", start_time=now - 40)
            old_run = make_fake_run(root, "old", start_time=now - 30)
            new_run = make_fake_run(root, "new", start_time=now - 20)
            current = make_fake_run(root, "current", start_time=now - 100)

            trace.cleanup_old_runs(workdir=root, current_run_id="current")

            suite.check("Sliding window deletes oldest run", not oldest.exists())
            suite.check("Sliding window deletes extra old run", not old_run.exists())
            suite.check("Sliding window keeps recent run", new_run.exists())
            suite.check("Current running run is preserved", current.exists())
        finally:
            restore_retention(old)


def test_missing_and_bad_metadata(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_cleanup_metadata_") as tmp:
        root = Path(tmp)
        old = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=1000,
            TRACE_RETENTION_MAX_RUNS=100,
            TRACE_RETENTION_MAX_MB=1000,
            TRACE_MAX_RUN_MB=1000,
            TRACE_KEEP_PINNED=True,
        )
        try:
            missing = make_fake_run(root, "missing_metadata", missing_metadata=True)
            bad = make_fake_run(root, "bad_metadata", bad_metadata=True)
            stats = trace.cleanup_old_runs(workdir=root)

            suite.check("Missing metadata does not crash cleanup", missing.exists())
            suite.check("Bad metadata does not crash cleanup", bad.exists())
            suite.check("Cleanup returns stats after metadata problems",
                        isinstance(stats, dict) and stats.get("run_count") == 2,
                        detail=str(stats))
        finally:
            restore_retention(old)


def test_large_run_reduction(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_cleanup_large_") as tmp:
        root = Path(tmp)
        old = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=1000,
            TRACE_RETENTION_MAX_RUNS=100,
            TRACE_RETENTION_MAX_MB=1000,
            TRACE_MAX_RUN_MB=0.001,
            TRACE_KEEP_PINNED=True,
        )
        try:
            run_dir = make_fake_run(
                root, "large_run", start_time=time.time(),
                trace_bytes=5000, artifacts_bytes=5000)

            trace.cleanup_old_runs(workdir=root)

            suite.check("Large run directory is not deleted", run_dir.exists())
            suite.check("Large run keeps metadata", (run_dir / "metadata.json").exists())
            suite.check("Large run keeps timeline.jsonl", (run_dir / "timeline.jsonl").exists())
            suite.check("Large run keeps timeline.md", (run_dir / "timeline.md").exists())
            suite.check("Large run keeps final.md", (run_dir / "final.md").exists())
            suite.check("Large run removes artifacts first", not (run_dir / "artifacts").exists())
            trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
            suite.check("Large run truncates full trace",
                        "Full trace truncated" in trace_text and len(trace_text) < 1000)
        finally:
            restore_retention(old)


def test_storage_quota(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_cleanup_quota_") as tmp:
        root = Path(tmp)
        old = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=1000,
            TRACE_RETENTION_MAX_RUNS=100,
            TRACE_RETENTION_MAX_MB=0.008,
            TRACE_MAX_RUN_MB=1000,
            TRACE_KEEP_PINNED=True,
        )
        try:
            now = time.time()
            oldest = make_fake_run(root, "quota_oldest", start_time=now - 40, trace_bytes=3000)
            newest = make_fake_run(root, "quota_newest", start_time=now, trace_bytes=3000)
            make_fake_run(root, "quota_middle_1", start_time=now - 30, trace_bytes=3000)
            make_fake_run(root, "quota_middle_2", start_time=now - 20, trace_bytes=3000)

            trace.cleanup_old_runs(workdir=root)
            stats = trace.get_trace_storage_stats(workdir=root)

            suite.check("Storage quota deletes oldest run first", not oldest.exists())
            suite.check("Storage quota keeps newest run when possible", newest.exists())
            suite.check("Storage stats are available", stats["run_count"] >= 1, detail=str(stats))
        finally:
            restore_retention(old)


def test_cleanup_failure_does_not_affect_start_run(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_cleanup_failure_") as tmp:
        root = Path(tmp)
        old_retention = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=0,
            TRACE_RETENTION_MAX_RUNS=0,
            TRACE_RETENTION_MAX_MB=0,
            TRACE_MAX_RUN_MB=1000,
            TRACE_KEEP_PINNED=True,
        )
        old_scan = trace._scan_runs
        try:
            def broken_scan(*args, **kwargs):
                raise RuntimeError("synthetic scan failure")

            trace._scan_runs = broken_scan
            run = trace.start_run(
                "cleanup failure smoke",
                workdir=root,
                model_provider="test",
                model="fake",
            )

            suite.check("Cleanup failure does not stop start_run", run.run_dir.exists())
            suite.check("Current run still has metadata", run.metadata_path.exists())
            suite.check("Current run still has trace", run.trace_path.exists())
            suite.check("Current run still has timeline", run.timeline_path.exists())
        finally:
            trace._scan_runs = old_scan
            trace.CURRENT_TRACE = None
            restore_retention(old_retention)


def test_run_index_state_machine(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_run_index_") as tmp:
        root = Path(tmp)
        old = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=1000,
            TRACE_RETENTION_MAX_RUNS=100,
            TRACE_RETENTION_MAX_MB=1000,
            TRACE_MAX_RUN_MB=1000,
            TRACE_KEEP_PINNED=True,
        )
        try:
            run = trace.start_run(
                "run index success test",
                workdir=root,
                model_provider="test",
                model="fake",
            )
            running = trace.get_run_summary(run.run_id, root)
            suite.check("start_run creates running index item",
                        bool(running and running["status"] == "running"))

            trace.record_tool_use(SimpleNamespace(
                name="read_file", id="toolu_1", input={"path": "README.md"}))
            trace.finish_run("done")
            success = trace.get_run_summary(run.run_id, root)
            suite.check("finish_run updates status to success",
                        success["status"] == "success")
            suite.check("tool_count increments on tool_use",
                        success["tool_count"] == 1)

            blocked_run = trace.start_run(
                "blocked test",
                workdir=root,
                model_provider="test",
                model="fake",
            )
            trace.record_hook("PreToolUse", tool="bash", tool_use_id="toolu_2",
                              input={"command": "rm -rf ."},
                              decision="blocked",
                              reason="Permission denied: delete commands are disabled")
            trace.finish_run("Permission denied: delete commands are disabled")
            blocked = trace.get_run_summary(blocked_run.run_id, root)
            suite.check("permission blocked run status is blocked",
                        blocked["status"] == "blocked")
            suite.check("blocked_count increments",
                        blocked["blocked_count"] == 1)

            failed_run = trace.start_run(
                "failed test",
                workdir=root,
                model_provider="test",
                model="fake",
            )
            trace.record_error(RuntimeError("boom"))
            trace.finish_run("[Error] RuntimeError: boom")
            failed = trace.get_run_summary(failed_run.run_id, root)
            suite.check("error run status is failed", failed["status"] == "failed")
            suite.check("error_count increments", failed["error_count"] == 1)
        finally:
            trace.CURRENT_TRACE = None
            restore_retention(old)


def test_corrupt_run_index_recovery(suite: CheckSuite):
    with tempfile.TemporaryDirectory(prefix="trace_run_index_corrupt_") as tmp:
        root = Path(tmp)
        index_path = root / ".codepilot" / "run_index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("{bad json", encoding="utf-8")
        try:
            run = trace.start_run(
                "after corrupt index",
                workdir=root,
                model_provider="test",
                model="fake",
            )
            item = trace.get_run_summary(run.run_id, root)
            suite.check("Corrupt run_index does not stop start_run",
                        bool(item and item["status"] == "running"))
            suite.check("Corrupt run_index is backed up",
                        bool(list(index_path.parent.glob("run_index.json.corrupt-*"))))
        finally:
            trace.CURRENT_TRACE = None


def test_real_runs_directory_not_touched(suite: CheckSuite):
    real_runs = PROJECT_ROOT / ".codepilot" / "runs"
    before = sorted(path.name for path in real_runs.iterdir()) if real_runs.exists() else []
    with tempfile.TemporaryDirectory(prefix="trace_cleanup_isolation_") as tmp:
        root = Path(tmp)
        old = patch_retention(
            TRACE_CLEANUP_ENABLED=True,
            TRACE_RETENTION_MAX_DAYS=0,
            TRACE_RETENTION_MAX_RUNS=0,
            TRACE_RETENTION_MAX_MB=0,
            TRACE_MAX_RUN_MB=1000,
            TRACE_KEEP_PINNED=True,
        )
        try:
            make_fake_run(root, "temp_only", start_time=time.time() - 100)
            trace.cleanup_old_runs(workdir=root)
        finally:
            restore_retention(old)
    after = sorted(path.name for path in real_runs.iterdir()) if real_runs.exists() else []
    suite.check("Real .codepilot/runs directory is not touched", before == after)


def main() -> int:
    print("Trace cleanup retention test")
    print(f"Project root: {PROJECT_ROOT}")
    print("All scenarios use temporary directories; real .codepilot/runs is not cleaned.")
    print()

    suite = CheckSuite()
    suite.run("ttl_and_pin", test_ttl_and_pin)
    suite.run("sliding_window_and_current_run", test_sliding_window_and_current_run)
    suite.run("missing_and_bad_metadata", test_missing_and_bad_metadata)
    suite.run("large_run_reduction", test_large_run_reduction)
    suite.run("storage_quota", test_storage_quota)
    suite.run("cleanup_failure_does_not_affect_start_run",
              test_cleanup_failure_does_not_affect_start_run)
    suite.run("run_index_state_machine", test_run_index_state_machine)
    suite.run("corrupt_run_index_recovery", test_corrupt_run_index_recovery)
    suite.run("real_runs_directory_not_touched", test_real_runs_directory_not_touched)
    return suite.finish()


if __name__ == "__main__":
    raise SystemExit(main())
