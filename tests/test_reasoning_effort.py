"""AQOURS_CODE_REASONING_EFFORT: the setting, the request fields it adds per
provider, DeepSeek's exception, and the reasoning tokens of a trace. No model
is called: the HTTP request is captured.

The taskgraph agents and the records (config.json, worker configuration, plan
report, gate output) are tested in
``tests/taskgraph/test_reasoning_effort_paths.py``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code import agent_loop, config, model_broker, trace
from aqours_code.config import REASONING_EFFORT_ENV, parse_reasoning_effort
from aqours_code.model_api import (REASONING_EFFORTS, OpenAICompatibleMessages,
                                   build_model_client, effective_reasoning_effort, is_openrouter,
                                   reasoning_effort_fields, reasoning_tokens)
from test_agent_loop_fake_model import FakeClient, install_common_agent_mocks, response, text_block

ROOT = Path(__file__).resolve().parents[1]
OPENROUTER = "https://openrouter.ai/api/v1"
OTHER = "https://example.invalid/v1"
DEEPSEEK_URL = "https://api.deepseek.com"
TOOLS = [{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object"}}]
TOOLS_OPENAI = [{"type": "function", "function": {"name": "read_file",
                                                  "description": "Read a file",
                                                  "parameters": {"type": "object"}}}]
MESSAGES = [{"role": "user", "content": "hi"}]
MESSAGES_OPENAI = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def env(value: str | None) -> dict:
    return {} if value is None else {REASONING_EFFORT_ENV: value}


# ── the setting ──


def test_unset_or_empty_means_no_thinking_setting():
    assert parse_reasoning_effort({}) == (None, "")
    assert parse_reasoning_effort(env("")) == (None, "")
    assert parse_reasoning_effort(env("   ")) == (None, "")
    assert parse_reasoning_effort(env('""')) == (None, "")
    # the test session runs with the variable empty (tests/conftest.py)
    assert (config.REASONING_EFFORT, config.REASONING_EFFORT_ERROR) == (None, "")
    assert config.configured_reasoning_effort() is None


@pytest.mark.parametrize("value, expected", [
    *((level, level) for level in ("low", "medium", "high", "xhigh", "max")),
    (" high ", "high"), ("HIGH", "high"), ("XHigh", "xhigh"), ('"max"', "max"), ("'low'", "low"),
])
def test_the_five_levels_are_accepted(value, expected):
    assert parse_reasoning_effort(env(value)) == (expected, "")
    assert REASONING_EFFORTS == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("value", ["none", "minimal", "hgh", "highest", "1", "true", "high,max",
                                   "high max", "default"])
def test_any_other_value_is_an_error(value):
    effort, error = parse_reasoning_effort(env(value))
    assert effort is None
    assert error == (f"AQOURS_CODE_REASONING_EFFORT must be one of low, medium, high, xhigh, "
                     f"max (or unset), got {value!r}")


def _invalid(monkeypatch, value: str = "hgh") -> str:
    error = parse_reasoning_effort(env(value))[1]
    monkeypatch.setattr(config, "REASONING_EFFORT_ERROR", error)
    return error


def test_an_invalid_value_stops_validation_and_every_use(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "BASE_URL", OPENROUTER)
    monkeypatch.setattr(config, "MODEL", "minimax/minimax-m3.1-flash")
    monkeypatch.setattr(config, "_CLIENT", None)
    config.validate_runtime_configuration()  # valid as it is
    _invalid(monkeypatch)
    message = "AQOURS_CODE_REASONING_EFFORT must be one of low, medium, high, xhigh, max"
    with pytest.raises(RuntimeError, match=message):
        config.validate_runtime_configuration()
    with pytest.raises(RuntimeError, match=message):
        config.configured_reasoning_effort()
    with pytest.raises(RuntimeError, match=message):
        config.model_reasoning_effort("openai_compatible", "minimax/minimax-m3.1-flash")
    # the model client is never built, so no request can be sent
    with pytest.raises(RuntimeError, match=message):
        config.client.messages
    assert config._CLIENT is None


def test_an_invalid_value_stops_the_agent_loop_before_any_call(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", "openai")
    _invalid(monkeypatch, "maximum")
    client = FakeClient([response([text_block("done")])])
    monkeypatch.setattr(agent_loop, "client", client)
    with pytest.raises(RuntimeError, match="got 'maximum'"):
        agent_loop.agent_loop([{"role": "user", "content": "hi"}], {})
    assert client.messages.calls == []


def _config_in_child(extra: dict) -> list:
    """The setting as a fresh process reads it from its environment."""
    code = ("import json; from aqours_code import config as c; print(json.dumps("
            "[c.REASONING_EFFORT, c.REASONING_EFFORT_ERROR]))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=60, env={**os.environ, **extra})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_environment_variable_reaches_config():
    # set (even empty), the variable is not taken from .env
    assert _config_in_child(env("")) == [None, ""]
    assert _config_in_child(env("high")) == ["high", ""]
    assert _config_in_child(env("High")) == ["high", ""]
    effort, error = _config_in_child(env("ultra"))
    assert effort is None and "got 'ultra'" in error


def test_the_model_client_gets_the_configured_level(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "BASE_URL", OPENROUTER)
    monkeypatch.setattr(config, "_CLIENT", None)
    monkeypatch.setattr(config, "REASONING_EFFORT", "high")
    assert config.get_model_client("openai_compatible").messages.reasoning_effort == "high"
    monkeypatch.setattr(config, "_CLIENT", None)
    monkeypatch.setattr(config, "REASONING_EFFORT", None)
    assert config.get_model_client("openai_compatible").messages.reasoning_effort is None


@pytest.mark.parametrize("provider", ["deepseek", "openai", "openai_compatible"])
def test_build_model_client_passes_the_level_on(provider):
    client = build_model_client(provider, api_key="test-key-not-real", base_url=OTHER,
                                reasoning_effort="medium")
    assert client.messages.reasoning_effort == "medium"
    assert build_model_client(provider, api_key="test-key-not-real",
                              base_url=OTHER).messages.reasoning_effort is None


def test_the_client_refuses_an_unknown_level():
    with pytest.raises(ValueError, match="invalid reasoning effort 'ultra'"):
        OpenAICompatibleMessages("test-key-not-real", OTHER, reasoning_effort="ultra")


# ── the requests ──


def capture(monkeypatch, usage: dict | None = None) -> list[dict]:
    """Capture the payload of every request instead of sending it."""
    payloads: list[dict] = []

    class FakeHttpResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            body = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
            if usage is not None:
                body["usage"] = usage
            return json.dumps(body).encode("utf-8")

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeHttpResponse()

    monkeypatch.setattr("aqours_code.model_api.urllib.request.urlopen", fake_urlopen)
    return payloads


def send(monkeypatch, *, provider: str, model: str, base_url: str, effort: str | None,
         tools=TOOLS, thinking: dict | None = None, max_tokens: int = 8000) -> dict:
    payloads = capture(monkeypatch)
    OpenAICompatibleMessages("test-key-not-real", base_url, provider_name=provider,
                             reasoning_effort=effort).create(
        model=model, system="sys", messages=MESSAGES, tools=tools, max_tokens=max_tokens,
        thinking=thinking)
    assert len(payloads) == 1
    return payloads[0]


# what each request was before the setting existed (and still is when it is unset)
BEFORE = [
    ("OpenAI-compatible provider", "minimax/minimax-m3.1-flash", OPENROUTER, None,
     {"model": "minimax/minimax-m3.1-flash", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
      "tools": TOOLS_OPENAI, "tool_choice": "auto"}),
    ("OpenAI", "gpt-test", OTHER, None,
     {"model": "gpt-test", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
      "tools": TOOLS_OPENAI, "tool_choice": "auto"}),
    ("DeepSeek", "deepseek-v4-flash", DEEPSEEK_URL, None,
     {"model": "deepseek-v4-flash", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
      "thinking": {"type": "enabled"}, "reasoning_effort": "high", "tools": TOOLS_OPENAI}),
    ("DeepSeek", "deepseek-v4-pro", DEEPSEEK_URL, {"type": "disabled"},
     {"model": "deepseek-v4-pro", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
      "thinking": {"type": "disabled"}, "tools": TOOLS_OPENAI}),
    ("DeepSeek", "deepseek-flash", DEEPSEEK_URL, None,
     {"model": "deepseek-flash", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
      "tools": TOOLS_OPENAI, "tool_choice": "auto"}),
    ("OpenAI-compatible provider", "anthropic/claude-sonnet-5.5", OPENROUTER, {"type": "disabled"},
     {"model": "anthropic/claude-sonnet-5.5", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
      "tools": TOOLS_OPENAI, "tool_choice": "auto"}),
]


@pytest.mark.parametrize("provider, model, base_url, thinking, expected", BEFORE,
                         ids=[f"{case[1]}@{case[2]}" for case in BEFORE])
def test_unset_every_request_is_exactly_as_before(monkeypatch, provider, model, base_url,
                                                  thinking, expected):
    assert send(monkeypatch, provider=provider, model=model, base_url=base_url, effort=None,
                thinking=thinking) == expected


@pytest.mark.parametrize("base_url", [OPENROUTER, "https://openrouter.ai/api/v1/",
                                      "https://OpenRouter.ai/api/v1", "https://eu.openrouter.ai/v1"])
@pytest.mark.parametrize("model", ["minimax/minimax-m3.1-flash", "anthropic/claude-sonnet-5.5",
                                   "qwen/qwen3.8-flash"])
def test_openrouter_gets_reasoning_effort_in_its_unified_field(monkeypatch, base_url, model):
    before = send(monkeypatch, provider="OpenAI-compatible provider", model=model,
                  base_url=base_url, effort=None)
    after = send(monkeypatch, provider="OpenAI-compatible provider", model=model,
                 base_url=base_url, effort="high")
    assert after == {**before, "reasoning": {"effort": "high"}}
    assert "reasoning_effort" not in after


@pytest.mark.parametrize("provider, base_url", [
    ("OpenAI-compatible provider", OTHER), ("OpenAI", "https://api.openai.com/v1"),
    ("OpenAI-compatible provider", "https://openrouter.ai.example.invalid/v1"),
    ("OpenAI-compatible provider", "https://example.invalid/openrouter.ai/v1"),
])
def test_other_providers_get_reasoning_effort(monkeypatch, provider, base_url):
    before = send(monkeypatch, provider=provider, model="some-model", base_url=base_url,
                  effort=None)
    after = send(monkeypatch, provider=provider, model="some-model", base_url=base_url,
                 effort="high")
    assert after == {**before, "reasoning_effort": "high"}
    assert "reasoning" not in after


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_every_level_is_sent_as_it_is(monkeypatch, effort):
    assert send(monkeypatch, provider="OpenAI-compatible provider", model="m", base_url=OPENROUTER,
                effort=effort)["reasoning"] == {"effort": effort}
    assert send(monkeypatch, provider="OpenAI", model="m", base_url=OTHER,
                effort=effort)["reasoning_effort"] == effort


def test_a_request_without_tools_gets_it_too(monkeypatch):
    payload = send(monkeypatch, provider="OpenAI-compatible provider", model="m",
                   base_url=OPENROUTER, effort="high", tools=None)
    assert payload == {"model": "m", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
                       "reasoning": {"effort": "high"}}


DEEPSEEK_CASES = [
    ("DeepSeek", "deepseek-v4-flash", DEEPSEEK_URL),
    ("DeepSeek", "deepseek-v4-pro", DEEPSEEK_URL),
    ("DeepSeek", "deepseek-flash", DEEPSEEK_URL),
    ("DeepSeek", "DeepSeek-Flash", DEEPSEEK_URL),
    ("DeepSeek", "deepseek-chat", DEEPSEEK_URL),       # provider deepseek, any model
    ("DeepSeek", "deepseek-flash", OPENROUTER),        # the provider decides, not the URL
    ("OpenAI-compatible provider", "deepseek-flash", DEEPSEEK_URL),  # a DeepSeek model name
    ("OpenAI-compatible provider", "deepseek-v4-flash", OPENROUTER),
]


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
@pytest.mark.parametrize("provider, model, base_url", DEEPSEEK_CASES,
                         ids=[f"{case[0]}:{case[1]}@{case[2]}" for case in DEEPSEEK_CASES])
@pytest.mark.parametrize("thinking", [None, {"type": "disabled"}])
def test_deepseek_is_not_affected(monkeypatch, provider, model, base_url, effort, thinking):
    before = send(monkeypatch, provider=provider, model=model, base_url=base_url, effort=None,
                  thinking=thinking)
    assert send(monkeypatch, provider=provider, model=model, base_url=base_url, effort=effort,
                thinking=thinking) == before


def test_deepseek_thinking_models_keep_high(monkeypatch):
    for effort in (None, "low", "max"):
        payload = send(monkeypatch, provider="DeepSeek", model="deepseek-v4-flash",
                       base_url=DEEPSEEK_URL, effort=effort)
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        assert "reasoning" not in payload and "tool_choice" not in payload


def test_a_request_that_turns_thinking_off_is_left_as_it_is(monkeypatch):
    # the compaction summary of a DeepSeek thinking model
    payload = send(monkeypatch, provider="DeepSeek", model="deepseek-v4-flash",
                   base_url=DEEPSEEK_URL, effort="max", tools=None, thinking={"type": "disabled"},
                   max_tokens=6000)
    assert payload == {"model": "deepseek-v4-flash", "messages": MESSAGES_OPENAI,
                       "max_tokens": 6000, "thinking": {"type": "disabled"}}
    # any other provider asked to turn it off gets no level either
    for base_url in (OPENROUTER, OTHER):
        payload = send(monkeypatch, provider="OpenAI-compatible provider", model="m",
                       base_url=base_url, effort="high", thinking={"type": "disabled"})
        assert "reasoning" not in payload and "reasoning_effort" not in payload
        assert "thinking" not in payload


@pytest.mark.parametrize("provider, provider_name, model, base_url, expected", [
    # compact.py turns thinking off for DeepSeek's thinking models only ...
    ("deepseek", "DeepSeek", "deepseek-v4-flash", DEEPSEEK_URL,
     {"thinking": {"type": "disabled"}}),
    # ... so the summaries of the other models ask the level like any request
    ("openai_compatible", "OpenAI-compatible provider", "minimax/minimax-m3.1-flash", OPENROUTER,
     {"reasoning": {"effort": "high"}}),
    ("openai", "OpenAI", "gpt-test", OTHER, {"reasoning_effort": "high"}),
])
def test_the_compaction_summary_request(monkeypatch, provider, provider_name, model, base_url,
                                        expected):
    from aqours_code import compact

    payloads = capture(monkeypatch)
    monkeypatch.setattr(compact, "client", SimpleNamespace(messages=OpenAICompatibleMessages(
        "test-key-not-real", base_url, provider_name=provider_name, reasoning_effort="high")))
    monkeypatch.setattr(compact, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(compact, "MODEL", model)
    assert compact._call_compact_model("summarize", runtime=None,
                                       purpose="compact_summary") == "done"
    assert payloads == [{"model": model, "messages": [{"role": "user", "content": "summarize"}],
                         "max_tokens": compact.SUMMARY_MAX_TOKENS, **expected}]


@pytest.mark.parametrize("base_url, expected", [
    (OPENROUTER, True), ("https://openrouter.ai", True), ("https://api.openrouter.ai/v1", True),
    (" 'https://openrouter.ai/api/v1' ", True), ("http://openrouter.ai:8080/v1", True),
    (OTHER, False), ("https://openrouter.ai.example.invalid", False),
    ("https://notopenrouter.ai/v1", False), ("", False), (None, False), ("not a url", False),
])
def test_is_openrouter(base_url, expected):
    assert is_openrouter(base_url) is expected


@pytest.mark.parametrize("provider, model, base_url, configured, expected", [
    ("openai_compatible", "minimax/minimax-m3.1-flash", OPENROUTER, "high", "high"),
    ("openai_compatible", "minimax/minimax-m3.1-flash", OPENROUTER, None, None),
    ("openai", "gpt-test", OTHER, "low", "low"),
    ("deepseek", "deepseek-v4-flash", DEEPSEEK_URL, None, "high"),
    ("deepseek", "deepseek-v4-pro", DEEPSEEK_URL, "max", "high"),
    ("deepseek", "deepseek-flash", DEEPSEEK_URL, "high", None),
    ("deepseek", "deepseek-chat", DEEPSEEK_URL, "high", None),
    ("openai_compatible", "deepseek-flash", OPENROUTER, "high", None),
    ("anthropic", "claude-sonnet-5.5", "", "high", None),
])
def test_the_recorded_level_is_the_one_the_requests_ask(provider, model, base_url, configured,
                                                        expected):
    assert effective_reasoning_effort(provider, model, base_url, configured) == expected
    fields = reasoning_effort_fields(provider, model, base_url, configured)
    if provider != "anthropic" and not provider.startswith("deepseek"):
        assert (expected is None) == (fields == {})


def test_config_records_the_level_of_its_model(monkeypatch):
    monkeypatch.setattr(config, "BASE_URL", OPENROUTER)
    monkeypatch.setattr(config, "REASONING_EFFORT", "high")
    assert config.model_reasoning_effort("openai_compatible", "minimax/minimax-m3.1-flash") == "high"
    assert config.model_reasoning_effort("deepseek", "deepseek-flash") is None
    assert config.model_reasoning_effort("deepseek", "deepseek-v4-flash") == "high"
    monkeypatch.setattr(config, "REASONING_EFFORT", None)
    assert config.model_reasoning_effort("openai_compatible", "minimax/minimax-m3.1-flash") is None


# ── reasoning tokens ──


@pytest.mark.parametrize("usage, expected", [
    ({"completion_tokens_details": {"reasoning_tokens": 1234}}, 1234),
    ({"completion_tokens_details": {"reasoning_tokens": 0}}, 0),
    (SimpleNamespace(completion_tokens_details={"reasoning_tokens": 7}), 7),
    (SimpleNamespace(completion_tokens_details=SimpleNamespace(reasoning_tokens=8)), 8),
    ({"output_tokens_details": {"reasoning_tokens": 9}}, 9),
    (SimpleNamespace(input_tokens=1, reasoning_tokens=3), 3),  # as the broker passes it on
    ({"completion_tokens_details": {"accepted_prediction_tokens": 0}}, None),
    ({"completion_tokens_details": None}, None),
    ({"completion_tokens_details": {"reasoning_tokens": "12"}}, None),
    ({"completion_tokens_details": {"reasoning_tokens": True}}, None),
    ({"prompt_tokens": 10, "completion_tokens": 5}, None),
    ({}, None), (None, None),
])
def test_reasoning_tokens(usage, expected):
    assert reasoning_tokens(usage) == expected


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def traced_usage(tmp_path: Path, monkeypatch, usage: dict | None) -> dict:
    """The ``usage`` of the trace's llm_response for a response with ``usage``."""
    capture(monkeypatch, usage)
    reply = OpenAICompatibleMessages("test-key-not-real", OPENROUTER,
                                     reasoning_effort="high").create(model="m", messages=MESSAGES)
    run = trace.start_run("prompt", workdir=tmp_path, model_provider="openai_compatible",
                          model="m")
    trace.record_llm_response(reply)
    events = [event for event in read_events(run.trace_path) if event["type"] == "llm_response"]
    assert len(events) == 1
    return events[0]["usage"]


def test_the_trace_records_reasoning_tokens_with_input_and_output(tmp_path, monkeypatch):
    usage = traced_usage(tmp_path, monkeypatch, {
        "prompt_tokens": 1200, "completion_tokens": 900, "total_tokens": 2100,
        "completion_tokens_details": {"reasoning_tokens": 750}})
    assert usage == {"input_tokens": 1200, "output_tokens": 900, "total_tokens": 2100,
                     "reasoning_tokens": 750}


def test_the_trace_records_no_reasoning_tokens_when_there_are_none(tmp_path, monkeypatch):
    usage = traced_usage(tmp_path, monkeypatch, {"prompt_tokens": 10, "completion_tokens": 5,
                                                 "total_tokens": 15})
    assert usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert traced_usage(tmp_path / "none", monkeypatch, None) == {}


def test_the_trace_keeps_anthropic_usage_as_it_was(tmp_path):
    reply = SimpleNamespace(content=[], stop_reason="end_turn", usage=SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
        cache_read_input_tokens=3))
    run = trace.start_run("prompt", workdir=tmp_path, model_provider="anthropic", model="c")
    trace.record_llm_response(reply)
    event = [e for e in read_events(run.trace_path) if e["type"] == "llm_response"][0]
    assert event["usage"] == {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3}


def test_the_broker_passes_reasoning_tokens_on_to_the_child(tmp_path):
    reply = SimpleNamespace(content=[], stop_reason="end_turn", usage=SimpleNamespace(
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        completion_tokens_details={"reasoning_tokens": 4}))
    payload = model_broker._response_payload(reply)
    assert payload["usage"]["reasoning_tokens"] == 4
    child = model_broker._response_object(json.loads(json.dumps(payload)))
    run = trace.start_run("prompt", workdir=tmp_path, model_provider="openai", model="m")
    trace.record_llm_response(child)
    event = [e for e in read_events(run.trace_path) if e["type"] == "llm_response"][0]
    assert event["usage"]["reasoning_tokens"] == 4
    assert (event["usage"]["input_tokens"], event["usage"]["output_tokens"]) == (10, 5)
    # without them the broker's usage is what it was
    plain = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    assert model_broker._response_usage(plain) == {
        "input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "total_tokens": 15}
