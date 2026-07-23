from pathlib import Path
from types import SimpleNamespace

from codepilot_s20 import agent_loop, basic_tools, context, prompts
from codepilot_s20.command_executor import LocalCommandExecutor
from codepilot_s20.runtime import AgentRuntime


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
        "kind": "acceptance",
    }], runtime=runtime_a)
    assert result == "Updated 1 todos (1 acceptance, 1 unverified)"
    assert runtime_a.state.todos[0]["id"] == "accept:1"
    assert runtime_b.state.todos == []


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
        "id": "accept:1",
        "content": "preserve behavior",
        "status": "pending",
        "kind": "acceptance",
    })

    live_context = context.update_context({}, [], runtime)
    prompt = prompts.assemble_system_prompt(live_context, runtime)

    assert live_context["memories"] == "RUNTIME_MEMORY"
    assert live_context["acceptance_todos"] == runtime.state.todos
    assert str(runtime.paths.workdir) in prompt
    assert "RUNTIME_MEMORY" in prompt
    assert "preserve behavior" in prompt
    assert "- read_file:" in prompt
    assert "- bash:" not in prompt


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
