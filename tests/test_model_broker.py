from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot_s20.model_broker import (
    BrokerModelClient,
    BrokerProtocolError,
    ModelBroker,
)
from codepilot_s20.eval_container_entry import main as container_entry_main
from evals import run_eval


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, block_id: str):
    return SimpleNamespace(
        type="tool_use", name=name, id=block_id, input={"path": "note.txt"})


class RecordingMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[text_block("brokered"), tool_block("read_file", "call_1")],
            stop_reason="tool_use",
        )


def test_broker_round_trip_supports_only_messages_create_and_cleans_files(tmp_path):
    nonce = uuid.uuid4().hex
    messages = RecordingMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="scripted").start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        response = client.messages.create(
            model="scripted",
            system="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"name": "read_file", "input_schema": {}}],
            max_tokens=123,
        )
    finally:
        stopped = broker.stop()

    assert stopped is True
    assert broker.call_count == 1
    assert messages.calls[0]["model"] == "scripted"
    assert messages.calls[0]["max_tokens"] == 123
    assert response.stop_reason == "tool_use"
    assert response.content[0].text == "brokered"
    assert response.content[1].name == "read_file"
    assert not list((tmp_path / "requests").glob("*.json"))
    assert not list((tmp_path / "responses").glob("*.json"))


def test_broker_client_rejects_extra_rpc_surface_before_writing(tmp_path):
    nonce = uuid.uuid4().hex
    client = BrokerModelClient(tmp_path, nonce, request_timeout=0.1)

    with pytest.raises(BrokerProtocolError, match="unsupported arguments"):
        client.messages.create(
            model="scripted", messages=[], temperature=0.5)

    assert not (tmp_path / "requests").exists()


@pytest.mark.parametrize("nonce", ["../escape", "short", "bad nonce value"])
def test_broker_nonce_cannot_escape_ipc_root(tmp_path, nonce):
    with pytest.raises(BrokerProtocolError, match="invalid broker nonce"):
        BrokerModelClient(tmp_path, nonce)


def test_broker_returns_model_errors_without_exposing_other_host_rpc(tmp_path):
    class FailingMessages:
        def create(self, **_kwargs):
            raise RuntimeError("model unavailable")

    nonce = uuid.uuid4().hex
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=FailingMessages()),
        allowed_model="scripted").start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        with pytest.raises(RuntimeError, match="model unavailable"):
            client.messages.create(model="scripted", messages=[])
    finally:
        broker.stop()

    assert broker.call_count == 1
    assert "model unavailable" in broker.last_error


def test_broker_rejects_wrong_model_without_calling_host_client(tmp_path):
    nonce = uuid.uuid4().hex
    messages = RecordingMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=2, max_total_tokens=24000,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        with pytest.raises(BrokerProtocolError, match="not allowed"):
            client.messages.create(
                model="unauthorized-expensive-model", messages=[],
                max_tokens=8000)
    finally:
        broker.stop()

    assert messages.calls == []
    assert broker.call_count == 0
    assert broker.rejected_count == 1


@pytest.mark.parametrize("max_tokens", [0, -1, 16001, 1_000_000_000])
def test_broker_rejects_invalid_token_limits_without_host_call(
    tmp_path, max_tokens,
):
    nonce = uuid.uuid4().hex
    messages = RecordingMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=2, max_total_tokens=24000,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        with pytest.raises(BrokerProtocolError, match="max_tokens"):
            client.messages.create(
                model="case-model", messages=[], max_tokens=max_tokens)
    finally:
        broker.stop()

    assert messages.calls == []
    assert broker.call_count == 0


def test_broker_accepts_normal_8000_and_16000_recovery_requests(tmp_path):
    nonce = uuid.uuid4().hex
    messages = RecordingMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=2, max_total_tokens=24000,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        client.messages.create(
            model="case-model", messages=[], max_tokens=8000)
        client.messages.create(
            model="case-model", messages=[], max_tokens=16000)
    finally:
        broker.stop()

    assert [call["model"] for call in messages.calls] == [
        "case-model", "case-model"]
    assert [call["max_tokens"] for call in messages.calls] == [8000, 16000]
    assert broker.call_count == 2
    assert broker.requested_token_count == 24000


def test_broker_rejects_calls_beyond_case_budget_without_host_call(tmp_path):
    nonce = uuid.uuid4().hex
    messages = RecordingMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=1, max_total_tokens=16000,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        client.messages.create(
            model="case-model", messages=[], max_tokens=8000)
        with pytest.raises(BrokerProtocolError, match="call limit"):
            client.messages.create(
                model="case-model", messages=[], max_tokens=8000)
    finally:
        broker.stop()

    assert len(messages.calls) == 1
    assert broker.call_count == 1


def test_broker_rejects_request_beyond_case_token_budget(tmp_path):
    nonce = uuid.uuid4().hex
    messages = RecordingMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=2, max_total_tokens=8000,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        client.messages.create(
            model="case-model", messages=[], max_tokens=8000)
        with pytest.raises(BrokerProtocolError, match="token budget"):
            client.messages.create(
                model="case-model", messages=[], max_tokens=8000)
    finally:
        broker.stop()

    assert len(messages.calls) == 1
    assert broker.requested_token_count == 8000


def test_eval_broker_budget_is_derived_from_trusted_case_metadata():
    assert run_eval.model_budgets_for_case({"max_turns": 3}) == (3, 32000)
    assert run_eval.model_budgets_for_case({
        "max_turns": 99,
        "max_model_calls": 2,
        "max_model_tokens": 24000,
    }) == (2, 24000)


def test_noninteractive_container_entry_runs_normal_agent_through_broker(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    ipc = tmp_path / "ipc"
    for path in (workspace, state, runtime, ipc):
        path.mkdir()
    (workspace / "info.txt").write_text(
        "Project code: ALPHA-42\nOwner: Eval Systems\nLaunch: September\n",
        encoding="utf-8",
    )
    nonce = uuid.uuid4().hex
    config = {
        "task": "read info",
        "workspace": str(workspace),
        "state_root": str(state),
        "runtime_root": str(runtime),
        "ipc_root": str(ipc),
        "broker_nonce": nonce,
        "model": "scripted-eval",
        "request_timeout": 2,
        "case_timeout_seconds": 10,
        "cleanup_grace": 1,
        "tool_policy": run_eval.DOCKER_EVAL_TOOL_POLICY,
    }
    config_path = runtime / "input.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    broker = ModelBroker(
        ipc, nonce, run_eval.ScriptedEvalClient("read_file_basic"),
        allowed_model="scripted-eval").start()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-runtime")
    old_cwd = Path.cwd()
    try:
        exit_code = container_entry_main(["--config", str(config_path)])
    finally:
        os.chdir(old_cwd)
        broker.stop()

    assert exit_code == 0
    result = json.loads((runtime / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert "ALPHA-42" in result["run_info"]["final_answer"]
    assert (runtime / "trace.jsonl").is_file()
    assert (runtime / "timeline.jsonl").is_file()
    assert (runtime / "timeline.md").is_file()
    assert (runtime / "metadata.json").is_file()
    assert (runtime / "final.md").is_file()
    assert "OPENAI_API_KEY" not in os.environ
    assert (workspace / ".git").is_dir()
