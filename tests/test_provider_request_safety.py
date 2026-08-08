from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code import agent_loop, model_api, recovery
from evals import run_eval
from aqours_code.model_api import (
    EmergencyLimitedModelClient,
    ProviderRequestSafetyLimitError,
)
from aqours_code.model_broker import BrokerModelClient, PROTOCOL_VERSION


class RecordingMessages:
    def __init__(self, *, fail_first: bool = False):
        self.calls = []
        self.fail_first = fail_first

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("HTTP 429: retry me")
        return SimpleNamespace(content=[], stop_reason="end_turn")


def test_direct_fuse_allows_100_and_stops_101_before_provider(monkeypatch):
    messages = RecordingMessages()
    events = []
    monkeypatch.setattr(
        model_api,
        "_record_emergency_stop",
        lambda **payload: events.append(payload),
    )
    client = EmergencyLimitedModelClient(
        SimpleNamespace(messages=messages), 100,
    )

    for _ in range(100):
        client.messages.create(
            model="scripted", messages=[], _aqours_purpose="lead",
        )
    with pytest.raises(
        ProviderRequestSafetyLimitError,
        match="provider_request_safety_limit",
    ) as caught:
        client.messages.create(
            model="scripted", messages=[], _aqours_purpose="verifier",
        )
    # Repeated callers see the same terminal failure without duplicate events.
    with pytest.raises(ProviderRequestSafetyLimitError):
        client.messages.create(
            model="scripted", messages=[], _aqours_purpose="final",
        )

    assert len(messages.calls) == 100
    assert client.provider_request_count == 100
    assert caught.value.next_purpose == "verifier"
    assert events == [{
        "limit": 100,
        "used_requests": 100,
        "purpose": "verifier",
    }]


def test_direct_fuse_counts_all_purposes_and_agent_retry(monkeypatch):
    messages = RecordingMessages(fail_first=True)
    client = EmergencyLimitedModelClient(
        SimpleNamespace(messages=messages), 100,
    )
    monkeypatch.setattr(recovery, "retry_delay", lambda _attempt: 0)

    recovery.with_retry(
        lambda: client.messages.create(
            model="scripted", messages=[], _aqours_purpose="lead",
        ),
        recovery.RecoveryState(),
    )
    for purpose in ("compact_summary", "verifier", "resolve", "final"):
        client.messages.create(
            model="scripted", messages=[], _aqours_purpose=purpose,
        )

    snapshot = client.provider_request_snapshot()
    assert len(messages.calls) == 6
    assert snapshot["provider_request_count"] == 6
    assert snapshot["purpose_request_counts"] == {
        "lead": 2,
        "compact_summary": 1,
        "verifier": 1,
        "resolve": 1,
        "final": 1,
    }


def test_broker_client_exposes_only_observational_request_snapshot(tmp_path):
    nonce = "nonce123456789012"
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    (stats_dir / "broker_stats.json").write_text(json.dumps({
        "version": PROTOCOL_VERSION,
        "nonce": nonce,
        "call_count": 11,
        "provider_request_count": 11,
        "request_count": 10,
        "emergency_max_provider_requests": 100,
        "emergency_stop_count": 0,
        "max_provider_retries": 1,
        "last_error": "must not be exposed",
    }), encoding="utf-8")
    client = BrokerModelClient(tmp_path, nonce)

    snapshot = client.provider_request_snapshot()

    assert snapshot["provider_request_count"] == 11
    assert snapshot["emergency_max_provider_requests"] == 100
    assert "last_error" not in snapshot


def test_agent_fuse_failure_never_reuses_prior_visible_text(
    tmp_path, monkeypatch,
):
    (tmp_path / "note.txt").write_text("evidence\n", encoding="utf-8")
    first = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="OLD ASSISTANT PREAMBLE"),
            SimpleNamespace(
                type="tool_use", name="read_file", id="read-1",
                input={"path": "note.txt"},
            ),
        ],
        stop_reason="tool_use",
    )
    second = SimpleNamespace(
        content=[SimpleNamespace(
            type="tool_use", name="read_file", id="read-2",
            input={"path": "note.txt"},
        )],
        stop_reason="tool_use",
    )
    messages = RecordingMessages()
    responses = [first, second]

    def scripted_create(**kwargs):
        messages.calls.append(kwargs)
        return responses.pop(0)

    messages.create = scripted_create
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setattr(agent_loop, "EMERGENCY_MAX_PROVIDER_REQUESTS", 2)

    result = agent_loop.run_agent_task(
        "Inspect note.txt and report it.",
        str(tmp_path),
        str(trace),
        model_client=SimpleNamespace(messages=messages),
        model_provider="scripted",
        model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert len(messages.calls) == 2
    assert result["final_answer"].startswith(
        "[Error] ProviderRequestSafetyLimitError: "
        "provider_request_safety_limit"
    )
    assert result["final_answer"] != "OLD ASSISTANT PREAMBLE"
    events = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    emergency = [event for event in events if event["type"] == "emergency_stop"]
    assert len(emergency) == 1
    assert emergency[0]["used_requests"] == 2
    metadata = json.loads(
        (Path(result["run_dir"]) / "metadata.json").read_text(
            encoding="utf-8",
        )
    )
    assert metadata["status"] == "failed"
