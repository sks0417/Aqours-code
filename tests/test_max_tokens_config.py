"""AQOURS_CODE_MAX_TOKENS and AQOURS_CODE_ESCALATED_MAX_TOKENS: the settings,
the DeepSeek exception, and the agent loop that uses them (no model is called).

The taskgraph agents (planner, gate, team, workers) are tested in
``tests/taskgraph/test_max_tokens_paths.py``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aqours_code import agent_loop, config
from aqours_code.config import (BUILTIN_ESCALATED_MAX_TOKENS, BUILTIN_MAX_TOKENS,
                                ESCALATED_MAX_TOKENS_ENV, MAX_TOKENS_ENV, parse_max_tokens)
from aqours_code.model_api import (DEEPSEEK_128K_OUTPUT_MODELS,
                                   DEEPSEEK_THINKING_ESCALATED_MAX_TOKENS,
                                   effective_escalated_max_tokens, effective_initial_max_tokens)
from test_agent_loop_fake_model import FakeClient, install_common_agent_mocks, response, text_block

ROOT = Path(__file__).resolve().parents[1]
DEEPSEEK_MODELS = sorted(DEEPSEEK_128K_OUTPUT_MODELS)


def settings(initial: str | None = None, escalated: str | None = None) -> dict:
    env = {}
    if initial is not None:
        env[MAX_TOKENS_ENV] = initial
    if escalated is not None:
        env[ESCALATED_MAX_TOKENS_ENV] = escalated
    return env


# ── the settings ──


def test_the_defaults_are_unchanged():
    assert (BUILTIN_MAX_TOKENS, BUILTIN_ESCALATED_MAX_TOKENS) == (8000, 16000)
    assert parse_max_tokens({}) == (8000, 16000, "")
    assert parse_max_tokens(settings("", "  ")) == (8000, 16000, "")
    # the test session runs with both variables empty (tests/conftest.py)
    assert (config.DEFAULT_MAX_TOKENS, config.ESCALATED_MAX_TOKENS, config.MAX_TOKENS_ERROR) == (
        8000, 16000, "")
    assert config.configured_max_tokens() == (8000, 16000)
    assert (agent_loop.DEFAULT_MAX_TOKENS, agent_loop.ESCALATED_MAX_TOKENS) == (8000, 16000)


@pytest.mark.parametrize("env, expected", [
    (settings("128000", "128000"), (128000, 128000)),
    (settings("50000", "90000"), (50000, 90000)),
    (settings(" 32000 ", '"64000"'), (32000, 64000)),
    (settings("4000"), (4000, 16000)),            # the escalated default is still above
    (settings(None, "200000"), (8000, 200000)),
    (settings("16000"), (16000, 16000)),           # equal is allowed
])
def test_both_settings_take_effect(env, expected):
    assert parse_max_tokens(env) == (*expected, "")


@pytest.mark.parametrize("value", ["abc", "0", "-5", "12.5", "1e5", "8_000", "+8000", "8000k",
                                   "true"])
def test_a_value_that_is_not_a_positive_whole_number_is_an_error(value):
    for env, name in ((settings(value, "200000"), MAX_TOKENS_ENV),
                      (settings("8000", value), ESCALATED_MAX_TOKENS_ENV)):
        initial, escalated, error = parse_max_tokens(env)
        assert error == f"{name} must be a positive whole number, got {value!r}"
        assert (initial, escalated) == (8000, 16000)


def test_the_escalated_limit_below_the_initial_one_is_an_error():
    assert parse_max_tokens(settings("90000", "50000"))[2] == (
        "AQOURS_CODE_ESCALATED_MAX_TOKENS (50000) must be at least "
        "AQOURS_CODE_MAX_TOKENS (90000)")
    # only the initial limit set above the escalated default: say to set both
    assert parse_max_tokens(settings("128000"))[2] == (
        "AQOURS_CODE_ESCALATED_MAX_TOKENS must be at least AQOURS_CODE_MAX_TOKENS "
        "(128000); it is not set, and its default is 16000: set "
        "AQOURS_CODE_ESCALATED_MAX_TOKENS as well")
    both = parse_max_tokens(settings("x", "0"))[2]
    assert MAX_TOKENS_ENV + " must be" in both and ESCALATED_MAX_TOKENS_ENV + " must be" in both


def test_an_invalid_setting_stops_validation_and_every_use(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(config, "MODEL", "some-model")
    config.validate_runtime_configuration()  # valid as it is
    error = parse_max_tokens(settings("abc"))[2]
    monkeypatch.setattr(config, "MAX_TOKENS_ERROR", error)
    with pytest.raises(RuntimeError, match="AQOURS_CODE_MAX_TOKENS must be a positive whole"):
        config.validate_runtime_configuration()
    with pytest.raises(RuntimeError, match="AQOURS_CODE_MAX_TOKENS must be a positive whole"):
        config.configured_max_tokens()
    with pytest.raises(RuntimeError, match="AQOURS_CODE_MAX_TOKENS must be a positive whole"):
        config.model_max_tokens("openai", "gpt-test")


def _config_in_child(env: dict) -> dict:
    """The settings as a fresh process reads them from its environment."""
    code = ("import json; from aqours_code import config as c; print(json.dumps("
            "[c.DEFAULT_MAX_TOKENS, c.ESCALATED_MAX_TOKENS, c.MAX_TOKENS_ERROR]))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=60, env={**os.environ, **env})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_environment_variables_reach_config():
    # set (even empty), a variable is not taken from .env
    assert _config_in_child(settings("", "")) == [8000, 16000, ""]
    assert _config_in_child(settings("128000", "128000")) == [128000, 128000, ""]
    assert _config_in_child(settings("50000", "90000")) == [50000, 90000, ""]
    initial, escalated, error = _config_in_child(settings("lots", ""))
    assert (initial, escalated) == (8000, 16000)
    assert error == "AQOURS_CODE_MAX_TOKENS must be a positive whole number, got 'lots'"


# ── DeepSeek is not affected ──


def _old_initial(provider, model, configured):
    """``effective_initial_max_tokens`` before the two settings existed."""
    if provider.casefold() == "deepseek" and model.casefold() in DEEPSEEK_128K_OUTPUT_MODELS:
        return max(configured, 128_000)
    return configured


def _old_escalated(provider, model, current, configured):
    deepseek = provider.casefold() == "deepseek" and model.casefold() in DEEPSEEK_128K_OUTPUT_MODELS
    return max(current, configured, 128_000 if deepseek else configured)


@pytest.mark.parametrize("provider, model", [
    *(("deepseek", model) for model in DEEPSEEK_MODELS), ("DeepSeek", "DeepSeek-Flash"),
    ("deepseek", "deepseek-chat"), ("openai", "gpt-test"), ("openai_compatible", "deepseek-flash"),
    ("anthropic", "claude-test"), ("openai_compatible", "minimax/minimax-m3.1-flash"),
])
def test_with_the_defaults_every_model_gets_the_limits_it_got_before(provider, model):
    initial = effective_initial_max_tokens(provider, model, configured_default_max_tokens=8000)
    assert initial == _old_initial(provider, model, 8000)
    for current in (initial, 8000, 16000, 128000, 256000):
        assert effective_escalated_max_tokens(
            provider, model, current_max_tokens=current, configured_escalated_max_tokens=16000,
        ) == _old_escalated(provider, model, current, 16000)


@pytest.mark.parametrize("model", DEEPSEEK_MODELS)
@pytest.mark.parametrize("initial, escalated", [(8000, 16000), (50000, 90000),
                                                (128000, 128000), (200000, 524288), (1, 1)])
def test_deepseek_ignores_both_settings(model, initial, escalated, monkeypatch):
    assert effective_initial_max_tokens(
        "deepseek", model, configured_default_max_tokens=initial) == 128_000
    assert effective_escalated_max_tokens(
        "deepseek", model, current_max_tokens=128_000,
        configured_escalated_max_tokens=escalated) == 128_000
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", initial)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", escalated)
    assert config.model_max_tokens("deepseek", model) == {"initial": 128_000,
                                                          "escalated": 128_000}
    assert DEEPSEEK_THINKING_ESCALATED_MAX_TOKENS == 128_000


def test_other_models_follow_the_settings(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", 128000)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", 128000)
    for provider, model in (("openai_compatible", "minimax/minimax-m3.1-flash"),
                            ("openai_compatible", "deepseek-flash"), ("deepseek", "deepseek-chat"),
                            ("anthropic", "claude-sonnet-5.5")):
        assert config.model_max_tokens(provider, model) == {"initial": 128000,
                                                            "escalated": 128000}
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", 50000)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", 90000)
    assert config.model_max_tokens("openai", "gpt-test") == {"initial": 50000, "escalated": 90000}


# ── the agent loop (the interactive agent and run_agent_task) ──


def _loop_limits(monkeypatch, provider: str, model: str, replies: list) -> list[int]:
    from aqours_code import recovery

    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(recovery, "PRIMARY_MODEL", model)
    client = FakeClient(replies)
    monkeypatch.setattr(agent_loop, "client", client)
    agent_loop.agent_loop([{"role": "user", "content": "long answer"}], {})
    return [call["max_tokens"] for call in client.messages.calls]


def cut_then_done():
    return [response([text_block("partial")], stop_reason="max_tokens"),
            response([text_block("complete")])]


def test_the_agent_loop_keeps_the_default_limits(monkeypatch):
    assert _loop_limits(monkeypatch, "openai", "gpt-test", cut_then_done()) == [8000, 16000]


def test_the_agent_loop_uses_the_configured_limits(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", 50000)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", 90000)
    assert _loop_limits(monkeypatch, "openai", "gpt-test", cut_then_done()) == [50000, 90000]


def test_the_agent_loop_returns_to_the_initial_limit_after_an_answer(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", 50000)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", 90000)
    from test_agent_loop_fake_model import tool_block
    replies = [response([text_block("partial")], stop_reason="max_tokens"),
               response([tool_block("todo_write", {"todos": []})], stop_reason="tool_use"),
               response([text_block("complete")])]
    assert _loop_limits(monkeypatch, "openai", "gpt-test", replies) == [50000, 90000, 50000]


def test_the_agent_loop_does_not_escalate_when_both_limits_are_equal(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", 128000)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", 128000)
    replies = [response([text_block("partial")], stop_reason="max_tokens")]
    assert _loop_limits(monkeypatch, "openai_compatible", "minimax/minimax-m3.1-flash",
                        replies) == [128000]


@pytest.mark.parametrize("model", DEEPSEEK_MODELS)
def test_the_agent_loop_keeps_deepseek_at_128000(monkeypatch, model):
    monkeypatch.setattr(config, "DEFAULT_MAX_TOKENS", 50000)
    monkeypatch.setattr(config, "ESCALATED_MAX_TOKENS", 90000)
    assert _loop_limits(monkeypatch, "deepseek", model, cut_then_done()) == [128000]


def test_an_invalid_setting_stops_the_agent_loop_before_any_call(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    monkeypatch.setattr(agent_loop, "MODEL_PROVIDER", "openai")
    monkeypatch.setattr(config, "MAX_TOKENS_ERROR", parse_max_tokens(settings("0"))[2])
    client = FakeClient(cut_then_done())
    monkeypatch.setattr(agent_loop, "client", client)
    with pytest.raises(RuntimeError, match="AQOURS_CODE_MAX_TOKENS must be a positive whole"):
        agent_loop.agent_loop([{"role": "user", "content": "long answer"}], {})
    assert client.messages.calls == []
