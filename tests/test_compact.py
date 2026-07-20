from types import SimpleNamespace

from codepilot_s20 import agent_loop, compact


def tool_exchange(name: str, contents: list[str], *,
                  tool_name: str = "read_file",
                  paths: list[str] | None = None):
    ids = [f"{name}_{index}" for index in range(len(contents))]
    paths = paths or [f"{tool_id}.py" for tool_id in ids]
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id,
             "name": tool_name,
             "input": ({"path": path} if tool_name == "read_file" else {})}
            for tool_id, path in zip(ids, paths)
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": content}
            for tool_id, content in zip(ids, contents)
        ]},
    ]


def result_contents(message):
    return [block["content"] for block in message["content"]
            if block.get("type") == "tool_result"]


def test_micro_compact_does_not_touch_eight_result_batch_below_trigger():
    contents = [f"file {index}\n" + "x" * 700 for index in range(8)]
    messages = [{"role": "user", "content": "inspect repository"},
                *tool_exchange("latest", contents)]

    compact.micro_compact(messages)

    assert result_contents(messages[-1]) == contents
    assert compact.estimate_size(messages) < compact.MICRO_COMPACT_TRIGGER


def test_prepare_context_preserves_eight_result_batch_below_trigger():
    contents = [f"file {index}\n" + "x" * 700 for index in range(8)]
    messages = [{"role": "user", "content": "inspect repository"},
                *tool_exchange("pipeline", contents)]

    agent_loop.prepare_context(messages)

    assert result_contents(messages[-1]) == contents


def test_micro_compact_preserves_recent_distinct_read_working_set():
    old = "old evidence\n" + "o" * 13000
    recent = "recent evidence\n" + "r" * 12000
    latest = "latest evidence\n" + "l" * 12000
    messages = [{"role": "user", "content": "complex task"},
                *tool_exchange("old", [old]),
                *tool_exchange("recent", [recent]),
                *tool_exchange("latest", [latest])]

    compact.micro_compact(messages)

    assert result_contents(messages[2]) == [old]
    assert result_contents(messages[4]) == [recent]
    assert result_contents(messages[6]) == [latest]


def test_distinct_read_working_set_has_a_bounded_path_limit(monkeypatch):
    monkeypatch.setattr(compact, "KEEP_RECENT_READ_PATHS", 3)
    contents = [f"evidence {index}\n" + str(index) * 1000
                for index in range(4)]
    messages = [{"role": "user", "content": "complex task"}]
    for index, content in enumerate(contents):
        messages.extend(tool_exchange(f"batch_{index}", [content]))

    compact.micro_compact(messages, trigger_size=1, target_size=1)

    assert "Earlier tool result compacted" in result_contents(messages[2])[0]
    assert result_contents(messages[4]) == [contents[1]]
    assert result_contents(messages[6]) == [contents[2]]
    assert result_contents(messages[8]) == [contents[3]]


def test_wide_source_batch_survives_narrow_followup_reads(monkeypatch):
    monkeypatch.setattr(compact, "KEEP_RECENT_READ_PATHS", 12)
    source_paths = [f"src/package/module_{index}.py" for index in range(12)]
    source_contents = [f"source {index}\n" + chr(97 + index) * 900
                       for index in range(12)]
    test_paths = [f"tests/test_{index}.py" for index in range(3)]
    test_contents = [f"test {index}\n" + str(index) * 700
                     for index in range(3)]
    messages = [
        {"role": "user", "content": "complex repository task"},
        *tool_exchange("sources", source_contents, paths=source_paths),
        *tool_exchange("tests", test_contents, paths=test_paths),
        *tool_exchange("followup", [source_contents[5]],
                       paths=[source_paths[5]]),
    ]

    compact.micro_compact(messages, trigger_size=1, target_size=1)

    retained_sources = result_contents(messages[2])
    assert retained_sources[0] == source_contents[0]
    assert retained_sources[11] == source_contents[11]
    assert "Duplicate read compacted" in retained_sources[5]
    assert result_contents(messages[4]) == test_contents
    assert result_contents(messages[6]) == [source_contents[5]]


def test_todo_and_reminder_do_not_displace_recent_working_batches(monkeypatch):
    monkeypatch.setattr(compact, "KEEP_RECENT_READ_PATHS", 2)
    old = "old evidence\n" + "o" * 1000
    recent = "recent evidence\n" + "r" * 1000
    todo = "todo updated\n" + "t" * 300
    latest = "latest evidence\n" + "l" * 1000
    messages = [
        {"role": "user", "content": "complex task"},
        *tool_exchange("old", [old]),
        *tool_exchange("recent", [recent]),
        *tool_exchange("todo", [todo], tool_name="todo_write"),
        {"role": "user", "content": "<reminder>Update your todos.</reminder>"},
        *tool_exchange("latest", [latest]),
    ]

    compact.micro_compact(messages, trigger_size=1, target_size=1)

    assert result_contents(messages[2]) != [old]
    assert result_contents(messages[4]) == [recent]
    assert result_contents(messages[9]) == [latest]


def test_micro_compact_prefers_identical_path_and_content_duplicates():
    independent = "independent evidence\n" + "i" * 1000
    duplicate = "same file evidence\n" + "d" * 1000
    latest = "latest evidence\n" + "l" * 700
    messages = [
        {"role": "user", "content": "complex task"},
        *tool_exchange("independent", [independent]),
        *tool_exchange(
            "duplicate_old", [duplicate], paths=["/workspace/src/item.py"]),
        *tool_exchange(
            "duplicate_new", [duplicate], paths=["/workspace/src/item.py"]),
        *tool_exchange("latest", [latest]),
    ]
    target = compact.estimate_size(messages) - 500

    compact.micro_compact(messages, trigger_size=1, target_size=target)

    assert result_contents(messages[2]) == [independent]
    assert "Duplicate read compacted" in result_contents(messages[4])[0]
    assert result_contents(messages[6]) == [duplicate]
    assert result_contents(messages[8]) == [latest]


def test_same_path_with_different_content_is_not_deduplicated():
    independent = "independent evidence\n" + "i" * 1000
    old_version = "old file version\n" + "a" * 1000
    new_version = "new file version\n" + "b" * 1000
    latest = "latest evidence\n" + "l" * 700
    messages = [
        {"role": "user", "content": "complex task"},
        *tool_exchange("independent", [independent]),
        *tool_exchange(
            "version_old", [old_version], paths=["/workspace/src/item.py"]),
        *tool_exchange(
            "version_new", [new_version], paths=["/workspace/src/item.py"]),
        *tool_exchange("latest", [latest]),
    ]
    target = compact.estimate_size(messages) - 500

    compact.micro_compact(messages, trigger_size=1, target_size=target)

    assert result_contents(messages[2]) == [independent]
    assert result_contents(messages[4]) != [old_version]
    assert result_contents(messages[6]) == [new_version]


def test_tool_result_budget_finds_latest_batch_before_reminder_and_converges(
        tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "TOOL_RESULTS_DIR", tmp_path / "tool-results")
    contents = [(f"result {index}\n" + str(index) * 8000) for index in range(4)]
    messages = [{"role": "user", "content": "inspect"},
                *tool_exchange("wide", contents),
                {"role": "user", "content": "<reminder>Update todos.</reminder>"}]

    compact.tool_result_budget(messages)

    budgeted = result_contents(messages[-2])
    assert sum(map(len, budgeted)) <= compact.TOOL_RESULT_BATCH_LIMIT
    assert all("Full output:" in content for content in budgeted)
    for index, original in enumerate(contents):
        path = tmp_path / "tool-results" / f"wide_{index}.txt"
        assert path.read_text(encoding="utf-8") == original


def test_snip_compact_keeps_small_histories_even_above_message_count():
    messages = [{"role": "user", "content": f"short {index}"}
                for index in range(60)]

    result = compact.snip_compact(messages)

    assert result == messages
    assert len(result) == 60


def test_compact_history_keeps_recent_tool_exchange_paired(tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(compact, "summarize_history", lambda messages: "checkpoint")
    messages = [
        {"role": "user", "content": f"history {index}"}
        for index in range(4)
    ]
    messages.extend(tool_exchange("paired", ["important result"]))
    messages.extend([
        {"role": "assistant", "content": [{"type": "text", "text": "reason"}]},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": [{"type": "text", "text": "next"}]},
        {"role": "user", "content": "more"},
    ])

    result = compact.compact_history(messages)

    assert result[0]["content"] == "[Compacted]\n\ncheckpoint"
    paired_use = next(index for index, message in enumerate(result)
                      if message.get("role") == "assistant"
                      and compact.message_has_tool_use(message))
    assert compact.is_tool_result_message(result[paired_use + 1])


def test_summary_prompt_preserves_contract_producers_and_assumptions(monkeypatch):
    captured = {}

    class Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[
                SimpleNamespace(type="text", text="checkpoint")
            ])

    monkeypatch.setattr(
        compact, "client", SimpleNamespace(messages=Messages()))

    result = compact.summarize_history([
        {"role": "user", "content": "fingerprint includes quantity"}
    ])

    prompt = captured["messages"][0]["content"]
    assert result == "checkpoint"
    assert "inspected file/symbol map" in prompt
    assert "normalized or fingerprint fields" in prompt
    assert "verified code facts from assumptions" in prompt
