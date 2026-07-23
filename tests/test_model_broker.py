from __future__ import annotations

import json
import os
import time
import urllib.error
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot_s20.model_broker import (
    BrokerIpcTimeout,
    BrokerModelClient,
    BrokerProtocolError,
    BrokerRemoteError,
    ModelBroker,
    broker_ipc_wait_timeout,
)
from codepilot_s20 import recovery
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


class UsageMessages:
    def create(self, **_kwargs):
        return SimpleNamespace(
            content=[text_block("metered")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=120,
                output_tokens=30,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=40,
            ),
        )


def test_broker_records_actual_provider_usage_and_forwards_it(tmp_path):
    nonce = uuid.uuid4().hex
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=UsageMessages()),
        allowed_model="scripted",
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        response = client.messages.create(
            model="scripted", messages=[], max_tokens=8000)
        snapshot = client.budget_snapshot()
    finally:
        broker.stop()

    assert response.usage.input_tokens == 120
    assert response.usage.total_tokens == 200
    assert snapshot["actual_input_token_count"] == 120
    assert snapshot["actual_output_token_count"] == 30
    assert snapshot["actual_cache_creation_input_token_count"] == 10
    assert snapshot["actual_cache_read_input_token_count"] == 40
    assert snapshot["actual_total_token_count"] == 200
    assert snapshot["usage_response_count"] == 1
    assert snapshot["usage_missing_response_count"] == 0


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
    assert broker.retry_count == 0
    assert "model unavailable" in broker.last_error


@pytest.mark.parametrize("first_error", [
    urllib.error.URLError(TimeoutError("SSL handshake timed out")),
    RuntimeError("Model request failed: HTTP 503: temporarily unavailable"),
])
def test_broker_retries_one_transient_provider_error_without_overlap(
    tmp_path, first_error,
):
    class FlakyMessages(RecordingMessages):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        def create(self, **kwargs):
            self.calls.append(kwargs)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if len(self.calls) == 1:
                    raise first_error
                return SimpleNamespace(
                    content=[text_block("recovered")], stop_reason="end_turn")
            finally:
                self.active -= 1

    nonce = uuid.uuid4().hex
    messages = FlakyMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=2, max_total_tokens=16000,
        provider_timeout=0.1, max_provider_retries=1,
        provider_retry_delay=0, delivery_grace=0.1,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        response = client.messages.create(
            model="case-model", messages=[], max_tokens=8000)
    finally:
        broker.stop()

    assert response.content[0].text == "recovered"
    assert len(messages.calls) == 2
    assert messages.max_active == 1
    assert broker.request_count == 1
    assert broker.call_count == 2
    assert broker.retry_count == 1
    assert broker.requested_token_count == 16000
    assert broker.rejected_count == 0
    assert broker.last_error == ""
    assert broker.last_provider_error


@pytest.mark.parametrize(("max_calls", "max_total_tokens", "blocked_reason"), [
    (1, 16000, "call_budget"),
    (2, 8000, "token_budget"),
])
def test_broker_does_not_retry_when_attempt_budget_is_exhausted(
    tmp_path, max_calls, max_total_tokens, blocked_reason,
):
    class TimeoutMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            raise urllib.error.URLError(TimeoutError("handshake timed out"))

    nonce = uuid.uuid4().hex
    messages = TimeoutMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=max_calls,
        max_total_tokens=max_total_tokens,
        provider_timeout=0.1, max_provider_retries=1,
        provider_retry_delay=0, delivery_grace=0.1,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        with pytest.raises(BrokerRemoteError) as caught:
            client.messages.create(
                model="case-model", messages=[], max_tokens=8000)
    finally:
        broker.stop()

    assert caught.value.error_kind == "provider_timeout"
    assert messages.calls == 1
    assert broker.call_count == 1
    assert broker.retry_count == 0
    assert broker.retry_skipped_reason == blocked_reason


def test_broker_does_not_retry_without_full_case_time_window(tmp_path):
    class TimeoutMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            raise urllib.error.URLError(TimeoutError("handshake timed out"))

    nonce = uuid.uuid4().hex
    messages = TimeoutMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", case_deadline=time.monotonic() + 0.5,
        max_calls=2, max_total_tokens=16000,
        provider_timeout=0.4, max_provider_retries=1,
        provider_retry_delay=0, delivery_grace=0.2,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        with pytest.raises(BrokerRemoteError) as caught:
            client.messages.create(
                model="case-model", messages=[], max_tokens=8000)
    finally:
        broker.stop()

    assert caught.value.error_kind == "provider_timeout"
    assert messages.calls == 1
    assert broker.call_count == 1
    assert broker.retry_count == 0
    assert broker.retry_skipped_reason == "case_deadline"


def test_broker_does_not_retry_permanent_provider_error(tmp_path):
    class UnauthorizedMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("Model request failed: HTTP 401: invalid key")

    nonce = uuid.uuid4().hex
    messages = UnauthorizedMessages()
    broker = ModelBroker(
        tmp_path, nonce, SimpleNamespace(messages=messages),
        allowed_model="case-model", max_calls=2, max_total_tokens=16000,
        provider_timeout=0.1, max_provider_retries=1,
        provider_retry_delay=0, delivery_grace=0.1,
    ).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=2)
    try:
        with pytest.raises(BrokerRemoteError) as caught:
            client.messages.create(
                model="case-model", messages=[], max_tokens=8000)
    finally:
        broker.stop()

    assert caught.value.error_kind == "provider_http_401"
    assert messages.calls == 1
    assert broker.call_count == 1
    assert broker.retry_count == 0


def test_agent_recovery_does_not_multiply_broker_managed_retries():
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise BrokerRemoteError(
            "provider_http_429", "rate limited", "request_1234567890")

    with pytest.raises(BrokerRemoteError):
        recovery.with_retry(fail, recovery.RecoveryState())

    assert calls == 1


def test_broker_client_classifies_missing_delivery_as_ipc_timeout(tmp_path):
    nonce = uuid.uuid4().hex
    client = BrokerModelClient(
        tmp_path, nonce, request_timeout=0.05, poll_interval=0.005)

    with pytest.raises(BrokerIpcTimeout, match="broker_ipc_timeout"):
        client.messages.create(model="case-model", messages=[])

    assert not list((tmp_path / "requests").glob("*.json"))
    assert not list((tmp_path / "responses").glob("*.json"))


def test_ipc_wait_window_covers_retry_backoff_and_delivery_grace():
    assert broker_ipc_wait_timeout(
        60, max_provider_retries=1, retry_delay=1, delivery_grace=5,
    ) == 126
    assert run_eval.model_broker_timeouts(60, 600) == (60, 126)
    assert run_eval.model_broker_timeouts(60, 100) == (60, 100)


@pytest.mark.parametrize(("message", "category"), [
    ("BrokerIpcTimeout: broker_ipc_timeout: no response", "broker_ipc_timeout"),
    ("provider_timeout: SSL handshake timed out", "provider_http_timeout"),
    ("CaseTimeoutError: Agent container exceeded the case deadline", "case_timeout"),
])
def test_broker_transport_failures_have_distinct_categories(message, category):
    assert run_eval.agent_failure_category(message) == category


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
    assert run_eval.model_budgets_for_case({}) == (32, 264000)
    assert run_eval.model_budgets_for_case({"max_turns": 3}) == (32, 264000)
    assert run_eval.model_budgets_for_case({
        "max_model_calls": 2,
        "max_model_tokens": 24000,
    }) == (2, 24000)


@pytest.mark.parametrize("message", [
    "BrokerProtocolError: model broker call limit exceeded",
    "BrokerProtocolError: model broker token budget exceeded",
])
def test_broker_budget_exhaustion_has_one_clear_failure_category(message):
    assert run_eval.agent_failure_category(message) == "budget_exhausted"


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
        "broker_request_timeout": 7,
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


def test_container_entry_failure_preserves_command_execution_metadata(
    tmp_path, monkeypatch,
):
    from codepilot_s20 import agent_loop

    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    ipc = tmp_path / "ipc"
    for path in (workspace, state, runtime, ipc):
        path.mkdir()
    config = {
        "task": "run then fail",
        "workspace": str(workspace),
        "state_root": str(state),
        "runtime_root": str(runtime),
        "ipc_root": str(ipc),
        "broker_nonce": uuid.uuid4().hex,
        "model": "scripted-eval",
        "request_timeout": 2,
        "broker_request_timeout": 7,
        "case_timeout_seconds": 10,
        "cleanup_grace": 1,
        "tool_policy": run_eval.DOCKER_EVAL_TOOL_POLICY,
    }
    config_path = runtime / "input.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    def fail_after_command(*_args, **kwargs):
        executor = kwargs["command_executor"]
        result = executor.execute(
            'python -c "print(123)"', workspace, timeout=5)
        assert result["timed_out"] is False
        raise RuntimeError("simulated broker failure")

    monkeypatch.setattr(agent_loop, "run_agent_task", fail_after_command)
    old_cwd = Path.cwd()
    try:
        exit_code = container_entry_main(["--config", str(config_path)])
    finally:
        os.chdir(old_cwd)

    assert exit_code == 1
    payload = json.loads((runtime / "result.json").read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert "simulated broker failure" in payload["error"]
    assert payload["execution"]["command_execution_count"] == 1
