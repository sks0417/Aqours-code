import json
import time

from codepilot_s20 import trace


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def read_index(workdir):
    return trace.load_run_index(workdir)


def make_fake_run(base, name, start_time=None, pinned=False,
                  bad_metadata=False, trace_bytes=0, artifacts_bytes=0):
    run_dir = base / ".codepilot" / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    if bad_metadata:
        (run_dir / "metadata.json").write_text("{bad json", encoding="utf-8")
    else:
        metadata = {"run_id": name, "start_time": start_time or time.time()}
        (run_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8")
    (run_dir / "timeline.jsonl").write_text("", encoding="utf-8")
    (run_dir / "timeline.md").write_text("# Timeline", encoding="utf-8")
    (run_dir / "final.md").write_text("final", encoding="utf-8")
    if trace_bytes:
        (run_dir / "trace.jsonl").write_text("x" * trace_bytes, encoding="utf-8")
    else:
        (run_dir / "trace.jsonl").write_text("", encoding="utf-8")
    if artifacts_bytes:
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "large.bin").write_text("y" * artifacts_bytes, encoding="utf-8")
    if pinned:
        (run_dir / ".keep").write_text("", encoding="utf-8")
    return run_dir


def with_trace_retention(**overrides):
    old = {
        "TRACE_CLEANUP_ENABLED": trace.TRACE_CLEANUP_ENABLED,
        "TRACE_RETENTION_MAX_DAYS": trace.TRACE_RETENTION_MAX_DAYS,
        "TRACE_RETENTION_MAX_RUNS": trace.TRACE_RETENTION_MAX_RUNS,
        "TRACE_RETENTION_MAX_MB": trace.TRACE_RETENTION_MAX_MB,
        "TRACE_MAX_RUN_MB": trace.TRACE_MAX_RUN_MB,
        "TRACE_KEEP_PINNED": trace.TRACE_KEEP_PINNED,
    }
    for key, value in overrides.items():
        setattr(trace, key, value)
    return old


def restore_trace_retention(old):
    for key, value in old.items():
        setattr(trace, key, value)


def test_start_run_creates_files_and_metadata(tmp_path):
    run = trace.start_run(
        "hello",
        workdir=tmp_path,
        model_provider="openai",
        model="deepseek-chat",
    )

    assert run.run_dir.exists()
    assert run.trace_path.exists()
    assert run.timeline_path.exists()
    assert run.timeline_md_path.exists()
    assert run.metadata_path.exists()
    assert run.final_path.exists()

    metadata = json.loads(run.metadata_path.read_text(encoding="utf-8"))
    assert metadata["run_id"] == run.run_id
    assert metadata["status"] == "running"
    assert metadata["prompt_preview"] == "hello"
    assert metadata["tool_count"] == 0
    assert metadata["error_count"] == 0
    assert metadata["blocked_count"] == 0
    assert metadata["event_count"] >= 1
    assert metadata["timeline_event_count"] >= 1
    assert metadata["trace_path"].endswith("trace.jsonl")
    assert metadata["timeline_path"].endswith("timeline.jsonl")
    assert metadata["timeline_md_path"].endswith("timeline.md")
    assert metadata["final_path"].endswith("final.md")
    assert metadata["pinned"] is False
    assert metadata["model_provider"] == "openai"
    assert metadata["model"] == "deepseek-chat"
    assert metadata["workdir"] == str(tmp_path)
    assert "api_key" not in metadata
    assert "key" not in metadata

    events = read_events(run.trace_path)
    assert events[0]["type"] == "user_prompt"
    assert events[0]["prompt"] == "hello"

    timeline = read_events(run.timeline_path)
    assert timeline[0]["type"] == "user_prompt"
    assert timeline[0]["prompt"] == "hello"
    assert "User Request" in run.timeline_md_path.read_text(encoding="utf-8")

    index = read_index(tmp_path)
    item = [entry for entry in index if entry["run_id"] == run.run_id][0]
    assert item["status"] == "running"
    assert item["prompt_preview"] == "hello"


def test_finish_run_writes_final_and_end_time(tmp_path):
    run = trace.start_run("prompt", workdir=tmp_path, model_provider="anthropic", model="claude")

    trace.finish_run("final answer")
    trace.finish_run("second answer")

    assert run.final_path.read_text(encoding="utf-8") == "final answer"
    metadata = json.loads(run.metadata_path.read_text(encoding="utf-8"))
    assert metadata["end_time"] is not None
    assert metadata["status"] == "success"
    assert metadata["duration_ms"] >= 0

    events = read_events(run.trace_path)
    assert [event["type"] for event in events].count("final_answer") == 1
    item = trace.get_run_summary(run.run_id, workdir=tmp_path)
    assert item["status"] == "success"
    assert item["tool_count"] == 0
    assert item["error_count"] == 0
    assert item["blocked_count"] == 0


def test_tool_result_is_truncated(tmp_path):
    run = trace.start_run("prompt", workdir=tmp_path, model_provider="openai", model="model")

    trace.record_tool_result("toolu_1", "read_file", "x" * 6000)

    events = read_events(run.trace_path)
    tool_result = [event for event in events if event["type"] == "tool_result"][0]
    assert tool_result["tool"] == "read_file"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert len(tool_result["content"]) < 5100
    assert "[truncated" in tool_result["content"]


def test_llm_events_and_tool_use_are_recorded(tmp_path):
    class Block:
        type = "tool_use"
        id = "toolu_1"
        name = "read_file"
        input = {"path": "README.md"}

    class Response:
        stop_reason = "tool_use"
        content = [Block()]

    run = trace.start_run("prompt", workdir=tmp_path, model_provider="openai", model="model")

    trace.record_llm_request(model="model", max_tokens=100, message_count=2, tool_count=3)
    trace.record_llm_response(Response())
    trace.record_tool_use(Block())

    events = read_events(run.trace_path)
    event_types = [event["type"] for event in events]
    assert "llm_request" in event_types
    assert "llm_response" in event_types
    assert "tool_use" in event_types
    assert events[-1]["input"] == {"path": "README.md"}
    assert trace.get_run_summary(run.run_id, tmp_path)["tool_count"] == 1


def test_timeline_records_tool_flow_and_filters_debug_events(tmp_path):
    class Block:
        type = "tool_use"
        id = "toolu_1"
        name = "read_file"
        input = {"path": "README.md"}

    class Response:
        stop_reason = "tool_use"
        content = [Block()]

    run = trace.start_run("prompt", workdir=tmp_path, model_provider="openai", model="model")

    trace.record_llm_request(model="model", max_tokens=100, message_count=2, tool_count=3)
    trace.record_llm_response(Response())
    trace.record_tool_use(Block())
    trace.record_hook("PreToolUse", tool="read_file", stage="before")
    trace.record_hook("PreToolUse", tool="read_file", decision="allowed")
    trace.record_hook("PostToolUse", tool="read_file")
    trace.record_hook("Stop")
    trace.record_tool_result("toolu_1", "read_file", "hello")
    trace.finish_run("done")

    timeline = read_events(run.timeline_path)
    assert [event["type"] for event in timeline] == [
        "user_prompt", "tool_use", "tool_result", "final_answer"]
    assert timeline[1]["tool"] == "read_file"
    assert timeline[1]["input"] == {"path": "README.md"}
    assert timeline[2]["status"] == "success"
    assert "LLM" not in run.timeline_md_path.read_text(encoding="utf-8")


def test_timeline_records_permission_block_and_redacts(tmp_path):
    class Block:
        type = "tool_use"
        id = "toolu_1"
        name = "bash"
        input = {"command": "Remove-Item file", "authorization": "Bearer abc123"}

    run = trace.start_run(
        "delete token=abc123",
        workdir=tmp_path,
        model_provider="openai",
        model="model",
    )

    trace.record_tool_use(Block())
    trace.record_hook("PreToolUse", tool="bash", stage="before")
    trace.record_hook("PreToolUse", tool="bash", decision="allowed")
    trace.record_hook("PreToolUse", tool="bash", tool_use_id="toolu_1",
                      input=Block.input, decision="blocked",
                      reason="Permission denied: token=abc123")
    trace.record_tool_result("toolu_1", "bash", "Permission denied: token=abc123")
    trace.finish_run("Permission denied: token=abc123")

    timeline = read_events(run.timeline_path)
    event_types = [event["type"] for event in timeline]
    assert "permission_blocked" in event_types
    blocked = [event for event in timeline if event["type"] == "permission_blocked"][0]
    assert blocked["tool"] == "bash"
    assert blocked["input"]["command"] == "Remove-Item file"
    assert blocked["input"]["authorization"] == "[REDACTED]"
    assert blocked["reason"] == "Permission denied: token=[REDACTED]"
    assert not any(event.get("decision") == "allowed" for event in timeline)
    assert [event for event in timeline if event["type"] == "tool_result"][0]["status"] == "failed"

    combined = (
        run.trace_path.read_text(encoding="utf-8")
        + run.timeline_path.read_text(encoding="utf-8")
        + run.timeline_md_path.read_text(encoding="utf-8")
        + run.final_path.read_text(encoding="utf-8")
    )
    assert "abc123" not in combined
    metadata = json.loads(run.metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "blocked"
    assert metadata["blocked_count"] == 1
    item = trace.get_run_summary(run.run_id, tmp_path)
    assert item["status"] == "blocked"
    assert item["blocked_count"] == 1


def test_run_status_failed_after_error(tmp_path):
    run = trace.start_run("error prompt", workdir=tmp_path,
                          model_provider="openai", model="model")

    trace.record_error(RuntimeError("boom"))
    trace.finish_run("[Error] RuntimeError: boom")

    metadata = json.loads(run.metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["error_count"] == 1
    item = trace.get_run_summary(run.run_id, tmp_path)
    assert item["status"] == "failed"
    assert item["error_count"] == 1


def test_run_index_pinned_reflects_keep_file(tmp_path):
    run = trace.start_run("pin me", workdir=tmp_path,
                          model_provider="openai", model="model")
    (run.run_dir / ".keep").write_text("", encoding="utf-8")
    trace.reconcile_run_index(tmp_path)

    item = trace.get_run_summary(run.run_id, tmp_path)
    assert item["pinned"] is True


def test_corrupt_run_index_does_not_crash(tmp_path):
    index_path = tmp_path / ".codepilot" / "run_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{bad json", encoding="utf-8")

    run = trace.start_run("after corrupt index", workdir=tmp_path,
                          model_provider="openai", model="model")

    item = trace.get_run_summary(run.run_id, tmp_path)
    assert item["status"] == "running"
    backups = list(index_path.parent.glob("run_index.json.corrupt-*"))
    assert backups


def test_cleanup_sliding_window_removes_old_runs(tmp_path):
    old = with_trace_retention(
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=1000,
        TRACE_RETENTION_MAX_RUNS=2,
        TRACE_RETENTION_MAX_MB=1000,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        now = time.time()
        make_fake_run(tmp_path, "run_oldest", now - 30)
        make_fake_run(tmp_path, "run_old", now - 20)
        make_fake_run(tmp_path, "run_new", now - 10)
        make_fake_run(tmp_path, "run_newest", now)
        trace.reconcile_run_index(tmp_path)

        trace.cleanup_old_runs(workdir=tmp_path)

        runs_dir = tmp_path / ".codepilot" / "runs"
        assert not (runs_dir / "run_oldest").exists()
        assert not (runs_dir / "run_old").exists()
        assert (runs_dir / "run_new").exists()
        assert (runs_dir / "run_newest").exists()
        index_ids = {item["run_id"] for item in trace.load_run_index(tmp_path)}
        assert "run_oldest" not in index_ids
        assert "run_old" not in index_ids
        assert "run_new" in index_ids
    finally:
        restore_trace_retention(old)


def test_cleanup_ttl_removes_expired_but_keeps_pinned(tmp_path):
    old = with_trace_retention(
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=1,
        TRACE_RETENTION_MAX_RUNS=100,
        TRACE_RETENTION_MAX_MB=1000,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        now = time.time()
        expired = make_fake_run(tmp_path, "expired", now - 3 * 24 * 60 * 60)
        pinned = make_fake_run(tmp_path, "pinned", now - 3 * 24 * 60 * 60, pinned=True)
        fresh = make_fake_run(tmp_path, "fresh", now)

        trace.cleanup_old_runs(workdir=tmp_path)

        assert not expired.exists()
        assert pinned.exists()
        assert fresh.exists()
    finally:
        restore_trace_retention(old)


def test_cleanup_bad_metadata_does_not_crash(tmp_path):
    old = with_trace_retention(
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=1000,
        TRACE_RETENTION_MAX_RUNS=100,
        TRACE_RETENTION_MAX_MB=1000,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        bad = make_fake_run(tmp_path, "bad_metadata", bad_metadata=True)

        stats = trace.cleanup_old_runs(workdir=tmp_path)

        assert bad.exists()
        assert stats["run_count"] == 1
    finally:
        restore_trace_retention(old)


def test_cleanup_storage_quota_removes_old_runs_first(tmp_path):
    old = with_trace_retention(
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=1000,
        TRACE_RETENTION_MAX_RUNS=100,
        TRACE_RETENTION_MAX_MB=0.008,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        now = time.time()
        oldest = make_fake_run(tmp_path, "quota_oldest", now - 30, trace_bytes=3000)
        make_fake_run(tmp_path, "quota_old", now - 20, trace_bytes=3000)
        make_fake_run(tmp_path, "quota_new", now - 10, trace_bytes=3000)
        newest = make_fake_run(tmp_path, "quota_newest", now, trace_bytes=3000)

        trace.cleanup_old_runs(workdir=tmp_path)

        assert not oldest.exists()
        assert newest.exists()
        stats = trace.get_trace_storage_stats(workdir=tmp_path)
        assert stats["total_mb"] <= 0.009
    finally:
        restore_trace_retention(old)


def test_cleanup_large_run_truncates_full_trace_not_timeline(tmp_path):
    old = with_trace_retention(
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=1000,
        TRACE_RETENTION_MAX_RUNS=100,
        TRACE_RETENTION_MAX_MB=1000,
        TRACE_MAX_RUN_MB=0.001,
        TRACE_KEEP_PINNED=True,
    )
    try:
        run_dir = make_fake_run(
            tmp_path, "large_run", time.time(),
            trace_bytes=5000, artifacts_bytes=5000)

        trace.cleanup_old_runs(workdir=tmp_path)

        assert run_dir.exists()
        assert (run_dir / "metadata.json").exists()
        assert (run_dir / "timeline.jsonl").exists()
        assert (run_dir / "timeline.md").exists()
        assert (run_dir / "final.md").exists()
        assert not (run_dir / "artifacts").exists()
        trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
        assert "Full trace truncated" in trace_text
        assert len(trace_text) < 1000
    finally:
        restore_trace_retention(old)


def test_start_run_cleanup_does_not_delete_current_run(tmp_path):
    old = with_trace_retention(
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=0,
        TRACE_RETENTION_MAX_RUNS=0,
        TRACE_RETENTION_MAX_MB=0,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        run = trace.start_run(
            "current",
            workdir=tmp_path,
            model_provider="openai",
            model="model",
        )

        assert run.run_dir.exists()
        assert run.metadata_path.exists()
        assert run.trace_path.exists()
        assert run.timeline_path.exists()
        assert run.timeline_md_path.exists()
        assert run.final_path.exists()
    finally:
        trace.CURRENT_TRACE = None
        restore_trace_retention(old)


def test_trace_write_failure_does_not_raise(tmp_path):
    run = trace.start_run("prompt", workdir=tmp_path, model_provider="openai", model="model")
    blocked_path = tmp_path / "not_a_directory"
    blocked_path.write_text("file", encoding="utf-8")
    run.run_dir = blocked_path
    run.trace_path = blocked_path / "trace.jsonl"

    run.event("error", message="ignored")
    run.finish("ignored")
