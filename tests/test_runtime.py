import hashlib
from pathlib import Path
from types import SimpleNamespace

from aqours_code import (
    agent_loop,
    basic_tools,
    context,
    mcp,
    prompts,
)
from aqours_code.command_executor import LocalCommandExecutor
from aqours_code.runtime import AgentRuntime


def make_runtime(
    workdir: Path,
    *,
    state_root: Path | None = None,
    tool_policy: dict | None = None,
) -> AgentRuntime:
    return AgentRuntime.create(
        workdir=workdir,
        state_root=state_root,
        model_client=SimpleNamespace(messages=object()),
        command_executor=LocalCommandExecutor(),
        model_provider="test",
        model="test-model",
        tool_policy=tool_policy,
        approval_mode="non_interactive",
        root_task="test task",
    )


def test_runtime_paths_and_mutable_state_are_isolated(tmp_path):
    runtime_a = make_runtime(tmp_path / "workspace-a", state_root=tmp_path / "state-a")
    runtime_b = make_runtime(tmp_path / "workspace-b", state_root=tmp_path / "state-b")

    runtime_a.state.todos.append({"content": "A", "status": "pending"})
    runtime_a.state.changed_files.add("a.py")
    runtime_a.state.lead_read_counts["a.py"] = 2

    assert runtime_b.state.todos == []
    assert runtime_b.state.changed_files == set()
    assert runtime_b.state.lead_read_counts == {}
    assert runtime_a.paths.memory_index == tmp_path / "state-a" / ".memory" / "MEMORY.md"
    assert runtime_b.paths.workdir == (tmp_path / "workspace-b").resolve()
    child = runtime_a.child()
    assert child.paths.state_root == runtime_a.paths.state_root
    assert child.state is not runtime_a.state


def test_explicit_runtime_controls_file_tools_and_todos(tmp_path, monkeypatch):
    runtime_a = make_runtime(tmp_path / "workspace-a")
    runtime_b = make_runtime(tmp_path / "workspace-b")
    runtime_a.paths.workdir.mkdir(parents=True)
    runtime_b.paths.workdir.mkdir(parents=True)
    poisoned_global = tmp_path / "legacy-global"
    poisoned_global.mkdir()
    monkeypatch.setattr(basic_tools, "WORKDIR", poisoned_global)

    assert "Wrote" in basic_tools.run_write(
        "value.txt", "runtime-a", runtime=runtime_a)
    assert basic_tools.run_read("value.txt", runtime=runtime_a) == "runtime-a"
    assert not (poisoned_global / "value.txt").exists()
    assert not (runtime_b.paths.workdir / "value.txt").exists()

    result = basic_tools.run_todo_write([{
        "content": "verify A",
        "status": "pending",
    }], runtime=runtime_a)
    assert result == "Updated 1 todos"
    assert runtime_a.state.todos[0]["id"] == "todo:1"
    assert runtime_b.state.todos == []


def test_read_observation_records_digest_without_parallel_working_memory(
    tmp_path, monkeypatch,
):
    runtime = make_runtime(tmp_path)
    source = tmp_path / "src" / "value.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(
        basic_tools,
        "record_event",
        lambda event_type, **data: events.append({
            "type": event_type, **data,
        }),
    )

    assert basic_tools.run_read(
        "./src/value.py",
        runtime=runtime,
        _tool_use_id="read-1",
    ) == "value = 1"

    assert not hasattr(runtime.state, "knowledge")
    assert events == [{
        "type": "read_observation",
        "tool_use_id": "read-1",
        "path": "src/value.py",
        "digest": hashlib.sha256(source.read_bytes()).hexdigest(),
        "offset": 0,
        "limit": None,
        "range_start": 0,
        "range_end": 1,
        "compact_generation": 0,
    }]


def test_context_and_prompt_read_runtime_owned_paths_and_policy(tmp_path):
    state_root = tmp_path / "state"
    memory_dir = state_root / ".memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("RUNTIME_MEMORY", encoding="utf-8")
    runtime = make_runtime(
        tmp_path / "workspace",
        state_root=state_root,
        tool_policy={
            "allowed_tools": ["read_file", "todo_write"],
            "allow_memory_context": True,
            "allow_skill_context": False,
            "allow_mcp": False,
            "allow_teammate_context": False,
        },
    )
    runtime.state.todos.append({
        "id": "todo:1",
        "content": "preserve behavior",
        "status": "pending",
    })

    live_context = context.update_context({}, [], runtime)
    prompt = prompts.assemble_system_prompt(live_context, runtime)

    assert live_context["memories"] == "RUNTIME_MEMORY"
    assert live_context["todos"] == runtime.state.todos
    assert str(runtime.paths.workdir) in prompt
    assert "RUNTIME_MEMORY" in prompt
    assert "preserve behavior" in prompt
    tool_names = {
        tool["name"] for tool in mcp.assemble_tool_pool(runtime)[0]
    }
    assert tool_names == {"read_file", "todo_write"}
    assert "API tool definitions and input schemas" in prompt
    assert "Read file contents." not in prompt


def test_run_agent_task_constructs_and_passes_explicit_runtime(
    tmp_path, monkeypatch,
):
    observed = {}

    def inspect(messages, live_context, runtime):
        observed["runtime"] = runtime
        observed["context"] = live_context
        messages.append({"role": "assistant", "content": [{
            "type": "text", "text": "done",
        }]})

    monkeypatch.setattr(agent_loop, "agent_loop", inspect)
    result = agent_loop.run_agent_task(
        "explicit task",
        str(tmp_path / "workspace"),
        model_client=SimpleNamespace(messages=object()),
        model_provider="test",
        model="test-model",
        command_executor=LocalCommandExecutor(),
        runtime_root=str(tmp_path / "state"),
    )

    runtime = observed["runtime"]
    assert runtime.state.root_task == "explicit task"
    assert runtime.paths.workdir == (tmp_path / "workspace").resolve()
    assert runtime.paths.state_root == (tmp_path / "state").resolve()
    assert runtime.services.trace_recorder is not None
    assert result["final_answer"] == "done"


def test_trace_storage_root_never_changes_runtime_state_paths(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    trusted_trace_root = tmp_path / "trusted-trace"
    observed = {}

    def inspect(messages, _live_context, runtime):
        observed["runtime"] = runtime
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
        })

    monkeypatch.setattr(agent_loop, "agent_loop", inspect)
    agent_loop.run_agent_task(
        "path ownership",
        str(workspace),
        model_client=SimpleNamespace(messages=object()),
        model_provider="test",
        model="test-model",
        command_executor=LocalCommandExecutor(),
        trace_storage_root=str(trusted_trace_root),
    )

    runtime = observed["runtime"]
    assert runtime.paths.workdir == workspace.resolve()
    assert runtime.paths.state_root == workspace.resolve()
    assert runtime.paths.skills_dir == workspace.resolve() / "skills"
    assert runtime.paths.memory_dir == workspace.resolve() / ".memory"
    assert runtime.paths.tasks_dir == workspace.resolve() / ".tasks"
    assert runtime.paths.worktrees_dir == workspace.resolve() / ".worktrees"
    assert not hasattr(runtime.paths, "context_archive_root")
    assert not hasattr(runtime.paths, "context_archive_dir")
    assert not (
        trusted_trace_root / ".aqours_code" / "context-archives"
    ).exists()


def test_explicit_runtime_root_still_owns_general_state(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    runtime_root = tmp_path / "runtime-state"
    trusted_trace_root = tmp_path / "trusted-trace"
    observed = {}

    def inspect(messages, _live_context, runtime):
        observed["runtime"] = runtime
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
        })

    monkeypatch.setattr(agent_loop, "agent_loop", inspect)
    agent_loop.run_agent_task(
        "explicit roots",
        str(workspace),
        model_client=SimpleNamespace(messages=object()),
        model_provider="test",
        model="test-model",
        command_executor=LocalCommandExecutor(),
        runtime_root=str(runtime_root),
        trace_storage_root=str(trusted_trace_root),
    )

    runtime = observed["runtime"]
    assert runtime.paths.state_root == runtime_root.resolve()
    assert runtime.paths.skills_dir.parent == runtime_root.resolve()
    assert runtime.paths.memory_dir.parent == runtime_root.resolve()
    assert runtime.paths.tasks_dir.parent == runtime_root.resolve()
    assert runtime.paths.worktrees_dir.parent == runtime_root.resolve()
    assert not hasattr(runtime.paths, "context_archive_root")
    assert not (
        trusted_trace_root / ".aqours_code" / "context-archives"
    ).exists()
