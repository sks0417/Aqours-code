"""AQOURS_CODE_OPENROUTER_PROVIDER: the setting, the field it adds to
OpenRouter requests only, DeepSeek's exception, and the provider that served
each reply in the trace. No model is called: the HTTP request is captured.

The taskgraph agents and the records (config.json, worker configuration, plan
report, gate output) are tested in
``tests/taskgraph/test_openrouter_provider_paths.py``.
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
from aqours_code.config import OPENROUTER_PROVIDER_ENV, parse_openrouter_provider
from aqours_code.model_api import (OpenAICompatibleMessages, build_model_client,
                                   effective_openrouter_provider, openrouter_provider_fields,
                                   served_provider)
from test_agent_loop_fake_model import FakeClient, install_common_agent_mocks, response, text_block
from test_reasoning_effort import BEFORE, DEEPSEEK_CASES, MESSAGES, MESSAGES_OPENAI

ROOT = Path(__file__).resolve().parents[1]
OPENROUTER = "https://openrouter.ai/api/v1"
OTHER = "https://example.invalid/v1"
DEEPSEEK_URL = "https://api.deepseek.com"
TOOLS = [{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object"}}]
Z_AI = {"provider": {"order": ["z-ai"], "allow_fallbacks": False}}


def env(value: str | None) -> dict:
    return {} if value is None else {OPENROUTER_PROVIDER_ENV: value}


# ── the setting ──


def test_unset_or_empty_pins_nothing():
    assert parse_openrouter_provider({}) == (None, "")
    assert parse_openrouter_provider(env("")) == (None, "")
    assert parse_openrouter_provider(env("   ")) == (None, "")
    assert parse_openrouter_provider(env("''")) == (None, "")
    # the test session runs with the variable empty (tests/conftest.py)
    assert (config.OPENROUTER_PROVIDER, config.OPENROUTER_PROVIDER_ERROR) == (None, "")
    assert config.configured_openrouter_provider() is None


@pytest.mark.parametrize("value, expected", [
    ("z-ai", ("z-ai",)),
    ("z-ai,deepinfra", ("z-ai", "deepinfra")),
    (" Z-AI , DeepInfra ", ("z-ai", "deepinfra")),
    ('"z-ai"', ("z-ai",)), ("'z-ai, novita'", ("z-ai", "novita")),
    ("z-ai/fp8", ("z-ai/fp8",)),                       # one endpoint of a provider
    ("particle-ai/nvfp4/flex", ("particle-ai/nvfp4/flex",)),
    ("google-vertex,amazon-bedrock", ("google-vertex", "amazon-bedrock")),
    ("deepinfra,z-ai", ("deepinfra", "z-ai")),         # the order is kept
])
def test_comma_separated_slugs_are_accepted(value, expected):
    assert parse_openrouter_provider(env(value)) == (expected, "")


@pytest.mark.parametrize("value", ["z-ai,", ",z-ai", ",", "z-ai,,deepinfra", "z ai", "z-ai;novita",
                                   "-z-ai", "z-ai/", "z-ai//fp8", "/fp8", '["z-ai"]', "z-ai\tnovita",
                                   "z-ai?x=1", "z_ai",
                                   # the display name, not the slug
                                   "Z.AI", "z-ai,Z.AI"])
def test_anything_else_is_an_error(value):
    providers, error = parse_openrouter_provider(env(value))
    assert providers is None
    assert error == ("AQOURS_CODE_OPENROUTER_PROVIDER must be comma-separated OpenRouter "
                     f"provider slugs such as z-ai (or unset), got {value.strip()!r}")


def _invalid(monkeypatch, value: str = "z-ai,") -> str:
    error = parse_openrouter_provider(env(value))[1]
    assert error
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ERROR", error)
    return error


def test_an_invalid_value_stops_validation_and_every_use(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "BASE_URL", OPENROUTER)
    monkeypatch.setattr(config, "MODEL", "z-ai/glm-5.3-flash")
    monkeypatch.setattr(config, "_CLIENT", None)
    config.validate_runtime_configuration()  # valid as it is
    _invalid(monkeypatch)
    message = "AQOURS_CODE_OPENROUTER_PROVIDER must be comma-separated OpenRouter provider slugs"
    with pytest.raises(RuntimeError, match=message):
        config.validate_runtime_configuration()
    with pytest.raises(RuntimeError, match=message):
        config.configured_openrouter_provider()
    with pytest.raises(RuntimeError, match=message):
        config.model_openrouter_provider("openai_compatible", "z-ai/glm-5.3-flash")
    # the model client is never built, so no request can be sent
    with pytest.raises(RuntimeError, match=message):
        config.client.messages
    assert config._CLIENT is None


def test_an_invalid_value_stops_the_agent_loop_before_any_call(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", "openai")
    _invalid(monkeypatch, "z ai")
    client = FakeClient([response([text_block("done")])])
    monkeypatch.setattr(agent_loop, "client", client)
    with pytest.raises(RuntimeError, match="got 'z ai'"):
        agent_loop.agent_loop([{"role": "user", "content": "hi"}], {})
    assert client.messages.calls == []


def _config_in_child(extra: dict) -> list:
    """The setting as a fresh process reads it from its environment."""
    code = ("import json; from aqours_code import config as c; print(json.dumps("
            "[c.OPENROUTER_PROVIDER, c.OPENROUTER_PROVIDER_ERROR]))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=60, env={**os.environ, **extra})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_environment_variable_reaches_config():
    # set (even empty), the variable is not taken from .env
    assert _config_in_child(env("")) == [None, ""]
    assert _config_in_child(env("z-ai")) == [["z-ai"], ""]
    assert _config_in_child(env("Z-AI, deepinfra")) == [["z-ai", "deepinfra"], ""]
    providers, error = _config_in_child(env("z-ai,"))
    assert providers is None and "got 'z-ai,'" in error


def test_the_model_client_gets_the_configured_providers(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "BASE_URL", OPENROUTER)
    monkeypatch.setattr(config, "_CLIENT", None)
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER", ("z-ai",))
    assert config.get_model_client("openai_compatible").messages.openrouter_provider == ("z-ai",)
    monkeypatch.setattr(config, "_CLIENT", None)
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER", None)
    assert config.get_model_client("openai_compatible").messages.openrouter_provider is None


@pytest.mark.parametrize("provider", ["deepseek", "openai", "openai_compatible"])
def test_build_model_client_passes_the_providers_on(provider):
    client = build_model_client(provider, api_key="test-key-not-real", base_url=OPENROUTER,
                                openrouter_provider=("z-ai", "novita"))
    assert client.messages.openrouter_provider == ("z-ai", "novita")
    assert build_model_client(provider, api_key="test-key-not-real",
                              base_url=OPENROUTER).messages.openrouter_provider is None


def test_the_client_refuses_a_bare_string():
    # "z-ai" would otherwise become ["z", "-", "a", "i"]
    with pytest.raises(TypeError, match="sequence of provider slugs"):
        OpenAICompatibleMessages("test-key-not-real", OPENROUTER, openrouter_provider="z-ai")


# ── the requests ──


def capture(monkeypatch, served: object = None) -> list[dict]:
    """Capture the payload of every request; the reply names ``served`` as its provider."""
    payloads: list[dict] = []

    class FakeHttpResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            body = {"id": "gen-1", "model": "z-ai/glm-5.3-flash",
                    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            if served is not None:
                body["provider"] = served
            return json.dumps(body).encode("utf-8")

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeHttpResponse()

    monkeypatch.setattr("aqours_code.model_api.urllib.request.urlopen", fake_urlopen)
    return payloads


def send(monkeypatch, *, provider: str, model: str, base_url: str, providers,
         effort: str | None = None, tools=TOOLS, thinking: dict | None = None,
         max_tokens: int = 8000) -> dict:
    payloads = capture(monkeypatch)
    OpenAICompatibleMessages("test-key-not-real", base_url, provider_name=provider,
                             reasoning_effort=effort, openrouter_provider=providers).create(
        model=model, system="sys", messages=MESSAGES, tools=tools, max_tokens=max_tokens,
        thinking=thinking)
    assert len(payloads) == 1
    return payloads[0]


@pytest.mark.parametrize("provider, model, base_url, thinking, expected", BEFORE,
                         ids=[f"{case[1]}@{case[2]}" for case in BEFORE])
def test_unset_every_request_is_exactly_as_before(monkeypatch, provider, model, base_url,
                                                  thinking, expected):
    for providers in (None, (), []):
        assert send(monkeypatch, provider=provider, model=model, base_url=base_url,
                    providers=providers, thinking=thinking) == expected


@pytest.mark.parametrize("base_url", [OPENROUTER, "https://openrouter.ai/api/v1/",
                                      "https://OpenRouter.ai/api/v1", "https://eu.openrouter.ai/v1"])
@pytest.mark.parametrize("model", ["z-ai/glm-5.3-flash", "minimax/minimax-m3.1-flash",
                                   "anthropic/claude-sonnet-5.5"])
def test_openrouter_requests_are_pinned_without_fallbacks(monkeypatch, base_url, model):
    before = send(monkeypatch, provider="OpenAI-compatible provider", model=model,
                  base_url=base_url, providers=None)
    after = send(monkeypatch, provider="OpenAI-compatible provider", model=model,
                 base_url=base_url, providers=("z-ai",))
    # the one field is added; nothing else changes
    assert after == {**before, **Z_AI}
    assert after["provider"]["allow_fallbacks"] is False


def test_the_order_of_several_providers_is_kept(monkeypatch):
    payload = send(monkeypatch, provider="OpenAI-compatible provider", model="z-ai/glm-5.3-flash",
                   base_url=OPENROUTER, providers=("z-ai", "deepinfra/fp4", "novita"))
    assert payload["provider"] == {"order": ["z-ai", "deepinfra/fp4", "novita"],
                                   "allow_fallbacks": False}


def test_the_field_goes_with_the_thinking_level_and_without_tools(monkeypatch):
    payload = send(monkeypatch, provider="OpenAI-compatible provider", model="m",
                   base_url=OPENROUTER, providers=("z-ai",), effort="high", tools=None)
    assert payload == {"model": "m", "messages": MESSAGES_OPENAI, "max_tokens": 8000,
                       "reasoning": {"effort": "high"}, **Z_AI}


def test_a_request_that_turns_thinking_off_is_still_pinned(monkeypatch):
    # routing is not thinking: the provider stays fixed for every call
    payload = send(monkeypatch, provider="OpenAI-compatible provider", model="m",
                   base_url=OPENROUTER, providers=("z-ai",), effort="high",
                   thinking={"type": "disabled"})
    assert payload["provider"] == Z_AI["provider"]
    assert "reasoning" not in payload and "thinking" not in payload


@pytest.mark.parametrize("provider, base_url", [
    ("OpenAI-compatible provider", OTHER), ("OpenAI", "https://api.openai.com/v1"),
    ("OpenAI-compatible provider", "https://openrouter.ai.example.invalid/v1"),
    ("OpenAI-compatible provider", "https://example.invalid/openrouter.ai/v1"),
    ("OpenAI-compatible provider", "http://localhost:8080/v1"),
])
def test_other_base_urls_are_not_affected(monkeypatch, provider, base_url):
    before = send(monkeypatch, provider=provider, model="some-model", base_url=base_url,
                  providers=None)
    assert send(monkeypatch, provider=provider, model="some-model", base_url=base_url,
                providers=("z-ai",)) == before


@pytest.mark.parametrize("provider, model, base_url", DEEPSEEK_CASES,
                         ids=[f"{case[0]}:{case[1]}@{case[2]}" for case in DEEPSEEK_CASES])
@pytest.mark.parametrize("thinking", [None, {"type": "disabled"}])
def test_deepseek_is_not_affected(monkeypatch, provider, model, base_url, thinking):
    before = send(monkeypatch, provider=provider, model=model, base_url=base_url, providers=None,
                  thinking=thinking)
    after = send(monkeypatch, provider=provider, model=model, base_url=base_url,
                 providers=("z-ai",), thinking=thinking)
    assert after == before and "provider" not in after


@pytest.mark.parametrize("provider, provider_name, model, base_url, pinned", [
    ("openai_compatible", "OpenAI-compatible provider", "z-ai/glm-5.3-flash", OPENROUTER, True),
    ("openai", "OpenAI", "gpt-test", OTHER, False),
    ("deepseek", "DeepSeek", "deepseek-v4-flash", DEEPSEEK_URL, False),
])
def test_the_compaction_summary_request(monkeypatch, provider, provider_name, model, base_url,
                                        pinned):
    from aqours_code import compact

    payloads = capture(monkeypatch)
    monkeypatch.setattr(compact, "client", SimpleNamespace(messages=OpenAICompatibleMessages(
        "test-key-not-real", base_url, provider_name=provider_name,
        openrouter_provider=("z-ai",))))
    monkeypatch.setattr(compact, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(compact, "MODEL", model)
    assert compact._call_compact_model("summarize", runtime=None,
                                       purpose="compact_summary") == "done"
    assert len(payloads) == 1
    assert (payloads[0].get("provider") == Z_AI["provider"]) is pinned
    assert ("provider" in payloads[0]) is pinned


@pytest.mark.parametrize("provider, model, base_url, configured, expected", [
    ("openai_compatible", "z-ai/glm-5.3-flash", OPENROUTER, ("z-ai",), ["z-ai"]),
    ("openai_compatible", "z-ai/glm-5.3-flash", OPENROUTER, ("z-ai", "novita"), ["z-ai", "novita"]),
    ("openai_compatible", "z-ai/glm-5.3-flash", OPENROUTER, None, None),
    ("openai_compatible", "z-ai/glm-5.3-flash", OTHER, ("z-ai",), None),
    ("openai", "gpt-test", "https://api.openai.com/v1", ("z-ai",), None),
    ("deepseek", "deepseek-v4-flash", DEEPSEEK_URL, ("z-ai",), None),
    ("deepseek", "deepseek-chat", OPENROUTER, ("z-ai",), None),
    ("openai_compatible", "deepseek-flash", OPENROUTER, ("z-ai",), None),
    ("anthropic", "claude-sonnet-5.5", OPENROUTER, ("z-ai",), None),
])
def test_the_recorded_providers_are_the_ones_the_requests_carry(provider, model, base_url,
                                                                configured, expected):
    assert effective_openrouter_provider(provider, model, base_url, configured) == expected
    if provider != "anthropic":
        fields = openrouter_provider_fields(provider, model, base_url, configured)
        assert fields == ({} if expected is None else
                          {"provider": {"order": expected, "allow_fallbacks": False}})


def test_config_records_the_providers_of_its_model(monkeypatch):
    monkeypatch.setattr(config, "BASE_URL", OPENROUTER)
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER", ("z-ai",))
    assert config.model_openrouter_provider("openai_compatible", "z-ai/glm-5.3-flash") == ["z-ai"]
    assert config.model_openrouter_provider("deepseek", "deepseek-flash") is None
    assert config.model_openrouter_provider("openai_compatible", "m", OTHER) is None
    monkeypatch.setattr(config, "BASE_URL", DEEPSEEK_URL)
    assert config.model_openrouter_provider("openai_compatible", "m") is None
    monkeypatch.setattr(config, "BASE_URL", OPENROUTER)
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER", None)
    assert config.model_openrouter_provider("openai_compatible", "z-ai/glm-5.3-flash") is None


# ── the provider that served each reply ──


@pytest.mark.parametrize("value, expected", [
    ("Z.AI", "Z.AI"), (" DeepInfra ", "DeepInfra"), ("", None), ("   ", None), (None, None),
    ({"name": "Z.AI"}, None), (["Z.AI"], None), (7, None), (True, None),
])
def test_served_provider(value, expected):
    assert served_provider(value) == expected


def reply_from(monkeypatch, served: object = None, base_url: str = OPENROUTER):
    capture(monkeypatch, served)
    return OpenAICompatibleMessages("test-key-not-real", base_url,
                                    openrouter_provider=("z-ai",)).create(model="m",
                                                                          messages=MESSAGES)


def test_the_reply_keeps_the_provider_the_response_names(monkeypatch):
    assert reply_from(monkeypatch, "Z.AI").provider == "Z.AI"
    assert reply_from(monkeypatch).provider is None
    assert reply_from(monkeypatch, {"name": "Z.AI"}).provider is None
    # whatever the base URL: it is what the response says
    assert reply_from(monkeypatch, "Novita", base_url=OTHER).provider == "Novita"


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def traced(tmp_path: Path, reply) -> dict:
    run = trace.start_run("prompt", workdir=tmp_path, model_provider="openai_compatible",
                          model="m")
    trace.record_llm_response(reply)
    events = [event for event in read_events(run.trace_path) if event["type"] == "llm_response"]
    assert len(events) == 1
    return events[0]


def test_the_trace_records_the_provider_of_each_call(tmp_path, monkeypatch):
    event = traced(tmp_path, reply_from(monkeypatch, "Z.AI"))
    assert event["provider"] == "Z.AI"
    # beside what the event had before
    assert event["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert event["stop_reason"] == "end_turn"


def test_the_trace_has_no_provider_when_the_response_names_none(tmp_path, monkeypatch):
    assert "provider" not in traced(tmp_path / "a", reply_from(monkeypatch))
    # an Anthropic SDK reply has no such attribute
    anthropic = SimpleNamespace(content=[], stop_reason="end_turn",
                                usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    assert "provider" not in traced(tmp_path / "b", anthropic)


def test_the_broker_passes_the_provider_on_to_the_child(tmp_path):
    reply = SimpleNamespace(content=[], stop_reason="end_turn", provider="Z.AI",
                            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    payload = model_broker._response_payload(reply)
    assert payload["provider"] == "Z.AI"
    child = model_broker._response_object(json.loads(json.dumps(payload)))
    assert child.provider == "Z.AI"
    assert traced(tmp_path, child)["provider"] == "Z.AI"
    # without one the broker's payload is what it was
    plain = SimpleNamespace(content=[], stop_reason="end_turn", provider=None,
                            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    assert "provider" not in model_broker._response_payload(plain)
    assert model_broker._response_object({"content": [], "stop_reason": "end_turn"}).provider is None
