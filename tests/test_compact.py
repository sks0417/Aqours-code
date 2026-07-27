from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot_s20 import compact, context
from codepilot_s20.command_executor import LocalCommandExecutor
from codepilot_s20.runtime import AgentRuntime


def make_runtime(tmp_path: Path, responses=()) -> AgentRuntime:
    class Messages:
        def __init__(self, values):
            self.values = list(values)
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            value = self.values.pop(0)
            if isinstance(value, BaseException):
                raise value
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=value)],
                usage=None,
            )

    return AgentRuntime.create(
        workdir=tmp_path,
        state_root=tmp_path / "state",
        model_client=SimpleNamespace(messages=Messages(responses)),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test",
        root_task="compact test",
    )


def exchange(index: int, result: str | None = None) -> list[dict]:
    tool_id = f"tool-{index}"
    return [
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_id,
                "name": "read_file",
                "input": {"path": f"src/{index}.py"},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result if result is not None else f"result-{index}",
            }],
        },
    ]


def long_history(count: int = 8, width: int = 350) -> list[dict]:
    messages = [{"role": "user", "content": "Keep the latest request exact."}]
    for index in range(count):
        messages.extend(exchange(index, f"fact-{index}-" + ("x" * width)))
    return messages


def render(messages: list) -> str:
    return json.dumps(messages, default=str, ensure_ascii=False)


def checkpoint_count(messages: list) -> int:
    return render(messages).count(compact.CONTEXT_CHECKPOINT_MARKER)


def archive_records(runtime):
    _, root = compact._archive_location(runtime, create=False)
    return compact._read_manifest(root / "manifest.jsonl")


def archived_path(runtime, record):
    _, root = compact._archive_location(runtime, create=False)
    return root / record["filename"]


def assert_tool_pairs(messages: list) -> None:
    for index, message in enumerate(messages):
        if not compact.is_tool_result_message(message):
            continue
        assert index > 0
        previous = messages[index - 1]
        assert compact.message_has_tool_use(previous)
        assert compact._tool_result_ids(message) <= compact._tool_use_ids(previous)


def install_summary(monkeypatch, text="## Progress\nOlder work retained."):
    calls = []

    def summarize(messages, runtime=None):
        calls.append(json.loads(json.dumps(messages)))
        return text

    monkeypatch.setattr(compact, "summarize_history", summarize)
    return calls


def force_compact(messages, **kwargs):
    return compact.compact_history(
        messages,
        reason="manual",
        target_context_budget=kwargs.pop("target_context_budget", 12_000),
        request_size_fn=kwargs.pop("request_size_fn", compact.estimate_size),
        **kwargs,
    )


def test_small_history_below_trigger_is_not_compacted(monkeypatch):
    messages = [{"role": "user", "content": "small request"}]
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: pytest.fail("summary must not run"),
    )

    result = compact.compact_history(messages, allow_model_summary=True)

    assert result is messages
    assert checkpoint_count(result) == 0


def test_successful_compact_has_one_markdown_checkpoint(monkeypatch):
    calls = install_summary(
        monkeypatch,
        "## Handoff\n- changed `src/a.py`\n- tests pass",
    )

    result = force_compact(long_history())

    assert len(calls) == 1
    assert checkpoint_count(result) == 1
    assert "## Handoff" in render(result)
    assert "semantic_memory_delta" not in render(result)


def test_compaction_prompt_requests_plain_markdown_and_includes_tool_facts(
    monkeypatch,
):
    captured = {}

    def invoke(prompt, **_kwargs):
        captured["prompt"] = prompt
        return "## Checkpoint\nUseful fact."

    monkeypatch.setattr(compact, "_call_compact_model", invoke)
    summary = compact.summarize_history(
        exchange(1, "reservation requires an idempotency key"),
    )

    assert summary.startswith("## Checkpoint")
    assert "reservation requires an idempotency key" in captured["prompt"]
    assert "Return concise Markdown only" in captured["prompt"]
    assert "JSON shape" not in captured["prompt"]
    assert "processed_tool_use_ids" not in captured["prompt"]


def test_prior_checkpoint_is_folded_into_next_without_stacking(monkeypatch):
    calls = install_summary(monkeypatch, "first cumulative checkpoint")
    first = force_compact(long_history(10))
    assert checkpoint_count(first) == 1

    first.extend(exchange(99, "new fact " + "y" * 800))
    calls.clear()
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda messages, runtime=None: (
            calls.append(json.loads(json.dumps(messages)))
            or "second cumulative checkpoint"
        ),
    )
    second = force_compact(first, target_context_budget=10_000)

    assert compact.CONTEXT_CHECKPOINT_MARKER in render(calls[0])
    assert checkpoint_count(second) == 1
    assert "second cumulative checkpoint" in render(second)


def test_recent_tail_tool_result_remains_verbatim(monkeypatch):
    install_summary(monkeypatch)
    messages = long_history(9, width=400)
    recent = exchange(50, "precise recent output")
    messages.extend(recent)

    result = force_compact(messages, target_context_budget=12_000)

    assert result[-2:] == recent
    assert_tool_pairs(result)


def test_latest_user_request_is_preserved_even_outside_suffix(monkeypatch):
    install_summary(monkeypatch)
    messages = [{"role": "user", "content": "LATEST USER REQUIREMENT"}]
    for index in range(12):
        messages.extend(exchange(index, "z" * 500))

    result = force_compact(messages, target_context_budget=10_000)

    assert "LATEST USER REQUIREMENT" in render(result)
    assert checkpoint_count(result) == 1


def test_cut_point_never_splits_tool_exchange(monkeypatch):
    install_summary(monkeypatch)

    result = force_compact(long_history(14, width=300), target_context_budget=10_000)

    assert_tool_pairs(result)


def test_multiple_tool_calls_near_cut_remain_paired(monkeypatch):
    install_summary(monkeypatch)
    multi_use = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "a", "name": "read_file",
             "input": {"path": "a.py"}},
            {"type": "tool_use", "id": "b", "name": "read_file",
             "input": {"path": "b.py"}},
        ],
    }
    multi_result = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "A"},
            {"type": "tool_result", "tool_use_id": "b", "content": "B"},
        ],
    }
    messages = long_history(8, width=300)
    messages.extend((multi_use, multi_result))

    result = force_compact(messages, target_context_budget=10_000)

    assert_tool_pairs(result)
    if multi_result in result:
        index = result.index(multi_result)
        assert result[index - 1] == multi_use


def test_summary_failure_keeps_original_history(monkeypatch):
    messages = long_history()
    original = json.loads(json.dumps(messages))
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    result = force_compact(messages)

    assert result is messages
    assert messages == original


def test_empty_summary_keeps_original_history(monkeypatch):
    messages = long_history()
    install_summary(monkeypatch, "   ")

    result = force_compact(messages)

    assert result is messages
    assert checkpoint_count(result) == 0


def test_second_compaction_overflow_never_drops_prior_checkpoint(monkeypatch):
    attempts = []
    messages = [
        {
            "role": "user",
            "content": (
                f"{compact.CONTEXT_CHECKPOINT_MARKER}\n"
                "CRITICAL PRIOR CHECKPOINT"
            ),
        },
        *long_history(12, width=400),
    ]
    original = json.loads(json.dumps(messages))

    def summarize(messages, runtime=None):
        attempts.append(json.loads(json.dumps(messages)))
        raise RuntimeError("context_length_exceeded")

    monkeypatch.setattr(compact, "summarize_history", summarize)

    result = force_compact(messages, target_context_budget=12_000)

    assert len(attempts) == 1
    assert "CRITICAL PRIOR CHECKPOINT" in render(attempts[0])
    assert result is messages
    assert messages == original


def test_compaction_uses_at_most_one_summary_model_call(monkeypatch):
    budget_requests = []
    monkeypatch.setattr(
        compact,
        "can_spend_optional_calls",
        lambda _client, calls: (
            budget_requests.append(calls) or (True, {"available": True})
        ),
    )
    scenarios = (
        lambda _messages, runtime=None: (_ for _ in ()).throw(
            RuntimeError("context_length_exceeded")
        ),
        lambda _messages, runtime=None: "s" * 30_000,
    )
    for summarize in scenarios:
        calls = 0

        def counted(messages, runtime=None):
            nonlocal calls
            calls += 1
            return summarize(messages, runtime)

        monkeypatch.setattr(compact, "summarize_history", counted)
        messages = long_history(15, width=400)
        original = json.loads(json.dumps(messages))
        force_compact(messages, target_context_budget=12_000)
        assert calls <= 1
        assert messages == original

    # An oversized pinned user instruction fails before the model call.
    calls = 0
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: pytest.fail("unsafe request must not be sent"),
    )
    impossible = [
        {"role": "user", "content": "PINNED-" + "p" * 60_000},
        *long_history(4),
    ]
    force_compact(impossible, target_context_budget=12_000)
    assert calls == 0
    assert budget_requests and set(budget_requests) == {1}


def test_small_prefix_tool_result_is_archived_before_removal(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch)
    exact = "small exact output"
    messages = [
        {"role": "user", "content": "archive old results"},
        *exchange(1, exact),
        *long_history(12, width=300)[1:],
    ]

    force_compact(messages, runtime=runtime, target_context_budget=12_000)

    record = next(
        item for item in archive_records(runtime)
        if item["tool_use_id"] == "tool-1"
    )
    assert archived_path(runtime, record).read_bytes() == exact.encode("utf-8")
    assert record["character_count"] == len(exact)
    assert record["sha256"] == hashlib.sha256(
        exact.encode("utf-8")
    ).hexdigest()


def test_rearchiving_same_tool_result_reuses_manifest_record(tmp_path):
    runtime = make_runtime(tmp_path)
    prefix = exchange(7, "same exact output")

    first, _ = compact._archive_prefix_tool_results(prefix, runtime)
    second, _ = compact._archive_prefix_tool_results(prefix, runtime)

    assert first[0]["archive_id"] == second[0]["archive_id"]
    assert first[0]["filename"] == second[0]["filename"]
    assert len(archive_records(runtime)) == 1


def test_large_result_middle_content_is_recoverable(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch)
    marker = "UNIQUE-MIDDLE-RECOVERY-MARKER"
    huge = "H" * 35_000 + marker + "T" * 35_000
    messages = [
        {"role": "user", "content": "preserve exact old output"},
        *exchange(18, huge),
        *long_history(12, width=300)[1:],
    ]

    result = force_compact(messages, runtime=runtime)

    record = next(
        item for item in archive_records(runtime)
        if item["tool_use_id"] == "tool-18"
    )
    assert marker in archived_path(runtime, record).read_text(encoding="utf-8")
    assert record["archive_id"] in render(result) or "archive://" in render(result)


def test_full_result_reaches_summary_when_request_fits(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    marker = "FULL-RESULT-MIDDLE-MARKER"
    output = "a" * 16_000 + marker + "b" * 16_000
    seen = []
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda messages, runtime=None: (
            seen.append(render(messages)) or "summary"
        ),
    )
    messages = [
        {"role": "user", "content": "summarize full result"},
        *exchange(20, output),
        *long_history(10, width=250)[1:],
    ]

    force_compact(messages, runtime=runtime, target_context_budget=12_000)

    assert len(seen) == 1
    assert marker in seen[0]
    assert "<archived-tool-result>" not in seen[0]


def test_only_summary_input_is_masked_when_budget_requires_it(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    marker = "MASKED-MIDDLE-IS-STILL-ON-DISK"
    huge = "a" * 35_000 + marker + "b" * 35_000
    messages = [
        {"role": "user", "content": "compact safely"},
        *exchange(30, huge),
        *long_history(10, width=250)[1:],
        *exchange(99, "RECENT VERBATIM RESULT"),
    ]
    original = json.loads(json.dumps(messages))
    seen = []
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda summary_input, runtime=None: (
            seen.append(render(summary_input)) or "masked summary"
        ),
    )

    result = force_compact(messages, runtime=runtime)

    assert "<archived-tool-result>" in seen[0]
    assert "tool_name: read_file" in seen[0]
    assert "tool_input:" in seen[0]
    assert "src/30.py" in seen[0]
    assert "sha256:" in seen[0]
    assert marker not in seen[0]
    assert messages == original
    assert "RECENT VERBATIM RESULT" in render(result[-2:])
    record = next(
        item for item in archive_records(runtime)
        if item["tool_use_id"] == "tool-30"
    )
    assert marker in archived_path(runtime, record).read_text(encoding="utf-8")


def test_checkpoint_contains_programmatic_archive_locator(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch, "summary without any path")
    messages = long_history(12, width=300)

    result = force_compact(messages, runtime=runtime)

    text = render(result)
    assert "## Archived tool results" in text
    assert "archive://" in text
    assert "read_archived_tool_result" in text


def test_archived_result_is_readable_by_agent_and_cannot_escape(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch)
    exact = "line one\nline two\nline three\n"
    messages = [
        {"role": "user", "content": "archive"},
        *exchange(44, exact),
        *long_history(10, width=300)[1:],
    ]
    force_compact(messages, runtime=runtime)

    from codepilot_s20.tool_defs import builtin_handlers
    archive_reader = builtin_handlers(runtime)["read_archived_tool_result"]
    archive_search = builtin_handlers(runtime)[
        "search_archived_tool_results"
    ]

    record = next(
        item for item in archive_records(runtime)
        if item["tool_use_id"] == "tool-44"
    )
    assert json.loads(archive_search(query="src/44.py"))[0][
        "archive_id"
    ] == record["archive_id"]
    assert archive_reader(archive_id=record["archive_id"]) == exact
    assert compact.read_archived_tool_result(
        archive_id=record["archive_id"],
        offset=1,
        limit=1,
        runtime=runtime,
    ) == "line two"
    latest = archive_reader(tool_use_id="tool-44")
    assert "Resolved latest archive_id=" in latest
    assert latest.endswith(exact)

    _, root = compact._archive_location(runtime, create=False)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    with (root / "manifest.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "archive_id": "escape-archive",
            "context_session_id": runtime.state.metadata[
                "context_session_id"
            ],
            "tool_use_id": "escape",
            "filename": str(outside),
            "sha256": hashlib.sha256(b"SECRET").hexdigest(),
        }) + "\n")
    escaped = compact.read_archived_tool_result(
        archive_id="escape-archive",
        runtime=runtime,
    )
    assert escaped == "Error: invalid archived tool result path"
    assert "SECRET" not in escaped


def test_active_run_archives_are_not_cleaned(tmp_path, monkeypatch):
    from codepilot_s20 import trace

    run = trace.start_run(
        "active archive",
        workdir=tmp_path,
        model_provider="test",
        model="test",
    )
    runtime = make_runtime(tmp_path)
    runtime.services.trace_recorder = run
    install_summary(monkeypatch)
    force_compact(
        [
            {"role": "user", "content": "archive during active run"},
            *exchange(55, "ACTIVE RUN EXACT RESULT"),
            *long_history(10, width=300)[1:],
        ],
        runtime=runtime,
    )
    _, archive_root = compact._archive_location(runtime, create=False)
    assert archive_root.exists()

    monkeypatch.setattr(trace, "TRACE_CLEANUP_ENABLED", True)
    monkeypatch.setattr(trace, "TRACE_RETENTION_MAX_DAYS", 0)
    monkeypatch.setattr(trace, "TRACE_RETENTION_MAX_RUNS", 0)
    monkeypatch.setattr(trace, "TRACE_RETENTION_MAX_MB", 0)
    trace.cleanup_old_runs(workdir=tmp_path, current_run_id=run.run_id)

    assert archive_root.exists()
    assert (archive_root / "manifest.jsonl").exists()


def test_context_archive_survives_trace_switch_and_is_discoverable(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    session_id = runtime.state.metadata["context_session_id"]
    runtime.services.trace_recorder = SimpleNamespace(run_id="run-1")
    exact = "OLD COMPACT.PY CONTENT\n" + ("detail\n" * 100)
    install_summary(monkeypatch, "summary without tool ids or paths")
    first_history = [
        {"role": "user", "content": "inspect compact"},
        *exchange(91, exact),
        *long_history(12, width=300)[1:],
    ]

    first = force_compact(first_history, runtime=runtime)
    runtime.services.trace_recorder = SimpleNamespace(run_id="run-2")
    first.extend(long_history(10, width=350)[1:])
    second = force_compact(
        first,
        runtime=runtime,
        target_context_budget=10_000,
    )

    assert runtime.state.metadata["context_session_id"] == session_id
    rendered = render(second)
    locator = f"archive://context/{session_id}/manifest.jsonl"
    assert rendered.count(locator) == 1
    found = json.loads(compact.search_archived_tool_results(
        query="src/91.py",
        tool_name="read_file",
        runtime=runtime,
    ))
    record = next(item for item in found if item["tool_use_id"] == "tool-91")
    assert compact.read_archived_tool_result(
        archive_id=record["archive_id"],
        runtime=runtime,
    ) == exact


def test_archive_locator_is_replaced_not_stacked(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    session_id = runtime.state.metadata["context_session_id"]
    old_locator = (
        "## Archived tool results\n\n"
        "Archive session: `archive://context/session-obsolete/manifest.jsonl`\n\n"
        "old instructions"
    )
    install_summary(monkeypatch, f"checkpoint\n\n{old_locator}")

    result = force_compact(
        long_history(12, width=300),
        runtime=runtime,
    )

    text = render(result)
    assert "session-obsolete" not in text
    assert text.count("## Archived tool results") == 1
    assert f"archive://context/{session_id}/manifest.jsonl" in text
    assert "search_archived_tool_results" in text


def test_context_archive_sessions_are_isolated(tmp_path):
    runtime_a = make_runtime(tmp_path)
    runtime_b = make_runtime(tmp_path)
    records, _ = compact._archive_prefix_tool_results(
        exchange(1, "secret-A"),
        runtime_a,
    )
    archive_id = records[0]["archive_id"]

    assert json.loads(compact.search_archived_tool_results(
        query=archive_id,
        runtime=runtime_b,
    )) == []
    result = compact.read_archived_tool_result(
        archive_id=archive_id,
        runtime=runtime_b,
    )
    assert result.startswith("Error:")
    assert "secret-A" not in result


def test_same_tool_id_can_have_distinct_exact_archive_versions(tmp_path):
    runtime = make_runtime(tmp_path)
    first, _ = compact._archive_prefix_tool_results(
        exchange(8, "version one"),
        runtime,
    )
    second, _ = compact._archive_prefix_tool_results(
        exchange(8, "version two"),
        runtime,
    )

    assert first[0]["archive_id"] != second[0]["archive_id"]
    assert compact.read_archived_tool_result(
        archive_id=first[0]["archive_id"],
        runtime=runtime,
    ) == "version one"
    assert compact.read_archived_tool_result(
        archive_id=second[0]["archive_id"],
        runtime=runtime,
    ) == "version two"
    latest = compact.read_archived_tool_result(
        tool_use_id="tool-8",
        runtime=runtime,
    )
    assert second[0]["archive_id"] in latest
    assert latest.endswith("version two")


@pytest.mark.parametrize("exact", [
    "line1\nline2\n",
    "line1\r\nline2\r\n",
    "no trailing newline",
    "中文与 code = '混合'\n",
])
def test_archive_preserves_exact_utf8_bytes_and_deduplicates(
    tmp_path,
    exact,
):
    runtime = make_runtime(tmp_path)
    first, _ = compact._archive_prefix_tool_results(
        exchange(70, exact),
        runtime,
    )
    second, _ = compact._archive_prefix_tool_results(
        exchange(70, exact),
        runtime,
    )
    record = first[0]

    assert archived_path(runtime, record).read_bytes() == exact.encode("utf-8")
    assert record["sha256"] == hashlib.sha256(
        exact.encode("utf-8")
    ).hexdigest()
    assert compact.read_archived_tool_result(
        archive_id=record["archive_id"],
        runtime=runtime,
    ) == exact
    assert second[0]["archive_id"] == record["archive_id"]
    assert len(archive_records(runtime)) == 1


def test_archive_search_matches_metadata_and_honors_limit(tmp_path):
    runtime = make_runtime(tmp_path)
    one, _ = compact._archive_prefix_tool_results(
        exchange(11, "first"),
        runtime,
    )
    two, _ = compact._archive_prefix_tool_results(
        exchange(12, "second"),
        runtime,
    )
    compact._archive_prefix_tool_results(
        exchange(11, "first-new-version"),
        runtime,
    )

    assert json.loads(compact.search_archived_tool_results(
        query="tool-11", runtime=runtime,
    ))
    assert json.loads(compact.search_archived_tool_results(
        query="src/12.py", runtime=runtime,
    ))[0]["archive_id"] == two[0]["archive_id"]
    assert json.loads(compact.search_archived_tool_results(
        query=one[0]["archive_id"], runtime=runtime,
    ))[0]["archive_id"] == one[0]["archive_id"]
    assert len(json.loads(compact.search_archived_tool_results(
        tool_name="read_file", limit=1, runtime=runtime,
    ))) == 1
    assert json.loads(compact.search_archived_tool_results(
        query="does-not-exist", runtime=runtime,
    )) == []


def test_archive_read_rejects_forged_paths_and_digest_mismatch(tmp_path):
    runtime = make_runtime(tmp_path)
    records, _ = compact._archive_prefix_tool_results(
        exchange(21, "trusted"),
        runtime,
    )
    record = records[0]
    archived_path(runtime, record).write_bytes(b"tampered")
    assert "digest mismatch" in compact.read_archived_tool_result(
        archive_id=record["archive_id"],
        runtime=runtime,
    )

    session_id, root = compact._archive_location(runtime, create=False)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    forged = [
        {
            "archive_id": "traversal",
            "context_session_id": session_id,
            "tool_use_id": "bad-1",
            "filename": "results/../outside.txt",
            "sha256": hashlib.sha256(b"outside secret").hexdigest(),
        },
        {
            "archive_id": "absolute",
            "context_session_id": session_id,
            "tool_use_id": "bad-2",
            "filename": str(outside.resolve()),
            "sha256": hashlib.sha256(b"outside secret").hexdigest(),
        },
    ]
    with (root / "manifest.jsonl").open("a", encoding="utf-8") as stream:
        for item in forged:
            stream.write(json.dumps(item) + "\n")

    for archive_id in ("traversal", "absolute", "../escape"):
        value = compact.read_archived_tool_result(
            archive_id=archive_id,
            runtime=runtime,
        )
        assert "outside secret" not in value
        assert value.startswith("Error:")
    assert compact.read_archived_tool_result(
        runtime=runtime,
    ).startswith("Error:")


def test_archive_read_rejects_symlink_result(tmp_path):
    runtime = make_runtime(tmp_path)
    session_id, root = compact._archive_location(runtime, create=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("symlink secret", encoding="utf-8")
    link = root / "results" / "linked.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    record = {
        "archive_id": "linked",
        "context_session_id": session_id,
        "tool_use_id": "linked-tool",
        "filename": "results/linked.txt",
        "sha256": hashlib.sha256(b"symlink secret").hexdigest(),
    }
    (root / "manifest.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )

    value = compact.read_archived_tool_result(
        archive_id="linked",
        runtime=runtime,
    )
    assert value == "Error: invalid archived tool result path"
    assert "symlink secret" not in value


@pytest.mark.parametrize("failure_point", [
    "mkdir",
    "result_write",
    "manifest_replace",
])
def test_archive_failure_keeps_history_and_skips_summary(
    tmp_path,
    monkeypatch,
    failure_point,
):
    runtime = make_runtime(tmp_path / failure_point)
    messages = long_history(12, width=350)
    original = json.loads(json.dumps(messages))
    summary_calls = []
    events = []
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: summary_calls.append(True) or "summary",
    )
    monkeypatch.setattr(
        compact,
        "record_event",
        lambda event_type, **payload: events.append({
            "type": event_type,
            **payload,
        }),
    )
    if failure_point == "mkdir":
        original_mkdir = Path.mkdir

        def fail_archive_mkdir(path, *args, **kwargs):
            if "context-archives" in path.parts:
                raise PermissionError("mkdir denied")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_archive_mkdir)
    elif failure_point == "result_write":
        original_write = compact._atomic_write_bytes

        def fail_result(path, raw):
            if path.suffix == ".txt":
                raise OSError("result full")
            return original_write(path, raw)

        monkeypatch.setattr(compact, "_atomic_write_bytes", fail_result)
    else:
        original_replace = compact.os.replace

        def fail_manifest(source, destination):
            if Path(destination).name == "manifest.jsonl":
                raise OSError("manifest replace failed")
            return original_replace(source, destination)

        monkeypatch.setattr(compact.os, "replace", fail_manifest)

    result = force_compact(messages, runtime=runtime)

    assert result is messages
    assert messages == original
    assert summary_calls == []
    failure = next(
        event for event in reversed(events)
        if event["type"] == "compact"
    )
    assert failure["success"] is False
    assert failure["archive_failed"] is True
    assert failure["archive_error_type"] in {"PermissionError", "OSError"}
    assert failure["summary_model_calls"] == 0


def test_partial_manifest_corruption_does_not_hide_valid_records(tmp_path):
    runtime = make_runtime(tmp_path)
    compact._archive_prefix_tool_results(
        exchange(31, "first valid"),
        runtime,
    )
    compact._archive_prefix_tool_results(
        exchange(32, "second valid"),
        runtime,
    )
    _, root = compact._archive_location(runtime, create=False)
    valid_lines = (root / "manifest.jsonl").read_text(
        encoding="utf-8",
    ).splitlines()
    (root / "manifest.jsonl").write_bytes(
        valid_lines[0].encode("utf-8")
        + b"\n{broken json\n\xff invalid utf8\n"
        + valid_lines[1].encode("utf-8")
        + b"\n"
    )

    found = json.loads(compact.search_archived_tool_results(
        tool_name="read_file",
        runtime=runtime,
    ))

    assert {item["tool_use_id"] for item in found} == {
        "tool-31",
        "tool-32",
    }
    for item in found:
        assert "valid" in compact.read_archived_tool_result(
            archive_id=item["archive_id"],
            runtime=runtime,
        )


def _make_archive_session(
    state_root: Path,
    session_id: str,
    *,
    updated_at: float,
    size: int = 0,
    keep: bool = False,
) -> Path:
    root = (
        state_root / ".codepilot" / "context-archives" / session_id
    )
    (root / "results").mkdir(parents=True)
    (root / "metadata.json").write_text(json.dumps({
        "context_session_id": session_id,
        "created_at": updated_at,
        "updated_at": updated_at,
    }), encoding="utf-8")
    if size:
        (root / "results" / "payload.txt").write_bytes(b"x" * size)
    if keep:
        (root / ".keep").write_text("", encoding="utf-8")
    return root


def test_context_archive_retention_protects_current_and_keep(tmp_path):
    state_root = tmp_path / "state"
    now = time.time()
    current = _make_archive_session(
        state_root, "session-current", updated_at=now - 10 * 86400,
    )
    pinned = _make_archive_session(
        state_root,
        "session-pinned",
        updated_at=now - 10 * 86400,
        keep=True,
    )
    expired = _make_archive_session(
        state_root, "session-expired", updated_at=now - 10 * 86400,
    )
    fresh = _make_archive_session(
        state_root, "session-fresh", updated_at=now,
    )

    compact.cleanup_context_archives(
        "session-current",
        state_root=state_root,
        max_age_days=1,
        max_sessions=10,
        max_total_mb=10,
    )

    assert current.exists()
    assert pinned.exists()
    assert fresh.exists()
    assert not expired.exists()


def test_context_archive_retention_count_quota_and_failure_are_safe(
    tmp_path,
    monkeypatch,
):
    state_root = tmp_path / "state"
    now = time.time()
    current = _make_archive_session(
        state_root, "session-current", updated_at=now, size=500,
    )
    newest = _make_archive_session(
        state_root, "session-newest", updated_at=now - 10, size=500,
    )
    oldest = _make_archive_session(
        state_root, "session-oldest", updated_at=now - 20, size=500,
    )

    compact.cleanup_context_archives(
        "session-current",
        state_root=state_root,
        max_age_days=365,
        max_sessions=2,
        max_total_mb=10,
    )
    assert current.exists()
    assert newest.exists()
    assert not oldest.exists()

    compact.cleanup_context_archives(
        "session-current",
        state_root=state_root,
        max_age_days=365,
        max_sessions=10,
        max_total_mb=0.0007,
    )
    assert current.exists()
    assert not newest.exists()

    doomed = _make_archive_session(
        state_root, "session-doomed", updated_at=now - 100,
    )
    monkeypatch.setattr(
        compact.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("busy")),
    )
    compact.cleanup_context_archives(
        "session-current",
        state_root=state_root,
        max_age_days=0,
        max_sessions=0,
        max_total_mb=0,
    )
    assert current.exists()
    assert doomed.exists()


def test_normal_tool_results_are_not_silently_removed_on_ordinary_turn(
    monkeypatch,
):
    messages = exchange(1, "ordinary precise output")
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda *_args, **_kwargs: pytest.fail("summary must not run"),
    )

    result = compact.compact_history(messages, allow_model_summary=True)

    assert result is messages
    assert "ordinary precise output" in render(result)


def test_compacted_request_satisfies_assembled_target(monkeypatch):
    install_summary(monkeypatch, "bounded checkpoint")
    system_overhead = 700

    def assembled_size(candidate):
        return compact.estimate_size(candidate) + system_overhead

    result = force_compact(
        long_history(15, width=300),
        target_context_budget=12_000,
        request_size_fn=assembled_size,
    )

    assert assembled_size(result) <= 12_000


def test_reactive_compact_forces_compaction_below_automatic_trigger(
    monkeypatch,
):
    calls = install_summary(monkeypatch, "reactive checkpoint")
    messages = long_history(8, width=200)

    result = compact.reactive_compact(
        messages,
        target_context_budget=10_000,
        request_size_fn=compact.estimate_size,
    )

    assert calls
    assert "reactive checkpoint" in render(result)


def test_update_context_does_not_inject_working_or_semantic_memory(tmp_path):
    runtime = make_runtime(tmp_path)

    live = context.update_context({}, [], runtime)

    assert "working_memory" not in live
    assert "working_memory_prompt" not in live
    assert "semantic_memory" not in live
    assert "semantic_memory_prompt" not in live


def test_transcript_and_compact_trace_record_required_metrics(
    tmp_path,
    monkeypatch,
):
    runtime = make_runtime(tmp_path)
    install_summary(monkeypatch, "trace checkpoint")
    events = []
    monkeypatch.setattr(
        compact,
        "record_event",
        lambda event_type, **payload: events.append({
            "type": event_type,
            **payload,
        }),
    )
    messages = long_history()

    force_compact(messages, runtime=runtime)

    compact_event = next(event for event in events if event["type"] == "compact")
    assert compact_event["reason"] == "manual"
    assert compact_event["before_messages"] == len(messages)
    assert compact_event["after_messages"] > 0
    assert compact_event["summarized_prefix_messages"] > 0
    assert compact_event["recent_tail_tokens"] > 0
    assert compact_event["summary_length"] > 0
    assert compact_event["success"] is True
    transcript = Path(compact_event["transcript"])
    assert transcript.exists()
    assert len(transcript.read_text(encoding="utf-8").splitlines()) == len(messages)


def test_two_consecutive_compactions_keep_one_checkpoint(monkeypatch):
    summaries = iter(("checkpoint one", "checkpoint two"))
    monkeypatch.setattr(
        compact,
        "summarize_history",
        lambda messages, runtime=None: next(summaries),
    )
    messages = long_history(10)
    messages[0]["content"] = [{
        "type": "text",
        "text": "Keep the latest list-form request exact.",
    }]
    first = force_compact(messages, target_context_budget=10_000)
    first.extend(long_history(5, width=250)[1:])

    second = force_compact(first, target_context_budget=9_000)

    assert checkpoint_count(second) == 1
    assert "checkpoint two" in render(second)
    assert_tool_pairs(second)
