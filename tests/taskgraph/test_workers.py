"""worker_entry (in process, fake model), AqoursWorker (fake entry), prompting, isolation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph.prompting import (
    IMPLEMENTED,
    IMPLEMENTED_UPSTREAM,
    INTERFACE_ONLY,
    AttemptFailure,
    build_node_prompt,
)
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest
from taskgraph_support import make_edge, make_graph, make_node

# ── worker_entry with a fake model client ──


def tool_use(name: str, call_id: str = "", **arguments):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use",
                                                    id=call_id or f"call_{name}",
                                                    name=name, input=arguments)],
                           stop_reason="tool_use",
                           usage=SimpleNamespace(input_tokens=11, output_tokens=3))


def final(text: str):
    # OpenAI-style usage names exercise the prompt/completion fallback.
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason="end_turn",
                           usage={"prompt_tokens": 7, "completion_tokens": 2})


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages = self

    def create(self, **_kwargs):
        self.calls += 1
        return self.responses.pop(0) if self.responses else final("done")


def worker_config(workspace: Path, log_dir: Path) -> dict:
    return {
        "node_id": "N", "attempt": 1, "task": "Write hello.txt.",
        "workspace": str(workspace), "log_dir": str(log_dir),
        "trace_path": str(log_dir / "trace_1.jsonl"),
        "result_path": str(log_dir / "worker_1.json"),
        "trace_storage_root": str(log_dir / "aqours_1" / "trace"),
        "runtime_root": str(log_dir / "aqours_1" / "state"),
        "timeout_s": 60,
        "model_provider": "scripted", "model": "scripted-model",
    }


def test_run_worker_writes_file_and_keeps_runtime_records_out_of_workspace(toy_repo, tmp_path):
    from aqours_code.taskgraph.worker_entry import run_worker

    log_dir = tmp_path / "log"
    client = FakeClient([tool_use("write_file", path="hello.txt", content="hi\n"),
                         final("done")])
    result = run_worker(worker_config(toy_repo.path, log_dir), model_client=client)
    assert result.ok, result
    assert (result.model_calls, result.input_tokens, result.output_tokens) == (2, 18, 5)
    assert (toy_repo.path / "hello.txt").read_text() == "hi\n"
    status = gitops.git(toy_repo.path, "status", "--porcelain").stdout.splitlines()
    assert status == ["?? hello.txt"]
    assert (log_dir / "trace_1.jsonl").is_file()
    assert any((log_dir / "aqours_1" / "trace").rglob("trace.jsonl"))


def test_run_worker_counts_every_call_without_a_limit(toy_repo, tmp_path):
    from aqours_code.taskgraph.worker_entry import run_worker

    client = FakeClient([
        tool_use("write_file", call_id="call_1", path="one.txt", content="1\n"),
        tool_use("write_file", call_id="call_2", path="two.txt", content="2\n"),
        final("done"),
    ])
    result = run_worker(worker_config(toy_repo.path, tmp_path / "log"), model_client=client)
    assert result.ok and result.reason == "", result
    assert result.model_calls == 3 and client.calls == 3
    assert (result.input_tokens, result.output_tokens) == (11 + 11 + 7, 3 + 3 + 2)
    assert (toy_repo.path / "one.txt").is_file() and (toy_repo.path / "two.txt").is_file()


def test_counting_client_exposes_no_budget():
    from aqours_code.taskgraph.worker_entry import CountingClient

    client = CountingClient(FakeClient([final("done")]))
    assert not hasattr(client, "budget_snapshot")
    response = client.messages.create(model="m", messages=[])
    assert response.stop_reason == "end_turn"
    assert (client.call_count, client.input_tokens, client.output_tokens) == (1, 7, 2)


def test_worker_tool_policy_is_minimal():
    from aqours_code.taskgraph.worker_entry import WORKER_TOOL_POLICY

    assert WORKER_TOOL_POLICY["allowed_tools"] == [
        "bash", "read_file", "write_file", "edit_file", "glob", "todo_write", "compact"]
    for key in ("allow_mcp", "allow_memory_context", "allow_skill_context",
                "allow_teammate_context", "background_tasks"):
        assert WORKER_TOOL_POLICY[key] is False
    assert "prompt_runtime" not in WORKER_TOOL_POLICY


# ── AqoursWorker with a fake entry script ──

FAKE_ENTRY = '''\
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
if args[0] == "--describe":
    print(json.dumps({"model_provider": "fake", "model": "fake-model"}))
    sys.exit(0)
config = json.loads(pathlib.Path(args[1]).read_text(encoding="utf-8"))
if config["task"] == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pathlib.Path(config["log_dir"], "grandchild.pid").write_text(str(child.pid))
    time.sleep(60)
answer = json.dumps({"cwd": os.getcwd(), "workdir": os.environ.get("AQOURS_CODE_WORKDIR"),
                     "config_keys": sorted(config)})
pathlib.Path(config["result_path"]).write_text(json.dumps(
    {"ok": True, "model_calls": 3, "input_tokens": 5, "output_tokens": 1,
     "final_answer": answer}), encoding="utf-8")
'''


def fake_worker(tmp_path: Path, **kwargs) -> AqoursWorker:
    script = tmp_path / "fake_entry.py"
    script.write_text(FAKE_ENTRY, encoding="utf-8")
    return AqoursWorker(entry_command=[sys.executable, str(script)],
                        **kwargs)


def request(tmp_path: Path, prompt: str, timeout_s: float = 30) -> WorkerRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return WorkerRequest(node_id="N", attempt=1, prompt=prompt, workspace=workspace,
                         log_dir=tmp_path / "log", timeout_s=timeout_s)


def test_aqours_worker_config_env_cwd_and_result(tmp_path):
    worker = fake_worker(tmp_path)
    req = request(tmp_path, "do it")
    result = worker.run(req)
    assert result.ok and result.exit_code == 0
    assert (result.model_calls, result.input_tokens, result.output_tokens) == (3, 5, 1)
    seen = json.loads(result.final_answer)
    assert Path(seen["cwd"]).resolve() == req.log_dir.resolve()
    assert Path(seen["workdir"]).resolve() == req.workspace.resolve()
    config = json.loads((req.log_dir / "worker_1_config.json").read_text(encoding="utf-8"))
    assert config["task"] == "do it" and "max_model_calls" not in config
    assert config["timeout_s"] == 30
    for key in ("trace_path", "result_path", "trace_storage_root", "runtime_root"):
        assert Path(config[key]).resolve().is_relative_to(req.log_dir.resolve())
    assert worker.describe() == {"worker": "aqours", "model_provider": "fake",
                                 "model": "fake-model"}


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        output = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True).stdout
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_aqours_worker_timeout_kills_process_tree(tmp_path):
    worker = fake_worker(tmp_path, kill_grace_s=0)
    req = request(tmp_path, "hang", timeout_s=2)
    started = time.monotonic()
    result = worker.run(req)
    assert time.monotonic() - started < 15
    assert not result.ok and result.reason == "worker_timeout"
    pid = int((req.log_dir / "grandchild.pid").read_text())
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(pid)


# ── prompting ──

def test_prompt_marks_interface_only_and_implemented_requirements(toy_index):
    graph = make_graph([
        make_node("C", kind="contract", modify=("models.py",),
                  provides=("models.py::JobStatus.FAILED",)),
        make_node("I", modify=("store.py",), provides=("store.py::JobStore.mark_failed",)),
        make_node("X", modify=("runner.py",),
                  requires=("models.py::JobStatus.FAILED", "store.py::JobStore.mark_failed",
                            "runner.py::MAX_ATTEMPTS"),
                  requires_impl=("store.py::JobStore.retry",),
                  context_files=("store.py",)),
    ], [make_edge("C", "I", "interface"), make_edge("I", "X")])
    by_id = {node.id: node for node in graph.nodes}
    prompt = build_node_prompt(graph, by_id["X"], toy_index, 1, None)
    assert f"`models.py::JobStatus.FAILED`: {INTERFACE_ONLY}" in prompt
    assert f"`store.py::JobStore.mark_failed`: {IMPLEMENTED}\n" in prompt
    assert f"`runner.py::MAX_ATTEMPTS`: {IMPLEMENTED}\n" in prompt
    assert f"`store.py::JobStore.retry`: {IMPLEMENTED_UPSTREAM}" in prompt
    assert graph.request in prompt and by_id["X"].goal in prompt
    assert "- `runner.py`" in prompt and "Do not modify, create, or delete any other file" in prompt
    assert "Previous attempt failed" not in prompt
    assert "Do not run git commands that change repository state" in prompt
    contract_prompt = build_node_prompt(graph, by_id["C"], toy_index, 1, None)
    assert "minimal default implementation" in contract_prompt
    assert "minimal default implementation" not in prompt


def test_retry_prompt_includes_reason_and_output_tail(toy_index):
    graph = make_graph([make_node("A", modify=("runner.py",))])
    output = "x" * 5000 + "TAIL-MARKER"
    prompt = build_node_prompt(graph, graph.nodes[0], toy_index, 2,
                               AttemptFailure("check_failed", output))
    assert "attempt 2" in prompt and "check_failed" in prompt
    assert "TAIL-MARKER" in prompt and "x" * 4100 not in prompt


# ── isolation ──

def test_coordinator_does_not_import_the_aqours_runtime():
    code = (
        "import sys\n"
        "import aqours_code.taskgraph.cli, aqours_code.taskgraph.coordinator\n"
        "import aqours_code.taskgraph.workers, aqours_code.taskgraph.prompting\n"
        "runtime = ['aqours_code.agent_loop', 'aqours_code.config', 'aqours_code.trace',\n"
        "           'aqours_code.runtime_state', 'aqours_code.command_executor']\n"
        "print([name for name in runtime if name in sys.modules])\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[2], timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
