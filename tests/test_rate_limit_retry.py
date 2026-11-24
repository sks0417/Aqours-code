"""HTTP 429 (rate limit): its own waits and retry count, in recovery.with_retry
and in the Docker model Broker, and the error left when the retries run out.

No model is called; a 429 is raised as the OpenAI-compatible client raises it
(a RuntimeError from the urllib HTTPError, which keeps the headers).
"""
from __future__ import annotations

import email.message
import email.utils
import io
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code import agent_loop, config, recovery, trace
from aqours_code.model_api import OpenAICompatibleMessages
from aqours_code.model_broker import (BrokerModelClient, BrokerRemoteError, ModelBroker,
                                      broker_ipc_wait_timeout)
from aqours_code.provider_errors import (DEFAULT_RATE_LIMIT_MAX_DELAY, DEFAULT_RATE_LIMIT_RETRIES,
                                         RATE_LIMIT_MAX_DELAY_ENV, RATE_LIMIT_RETRIES_ENV,
                                         RateLimitRetriesExhausted, is_rate_limit_error,
                                         parse_rate_limit, parse_retry_after, provider_message,
                                         rate_limit_wait, retry_after_seconds)
from test_agent_loop_fake_model import FakeClient, install_common_agent_mocks, response, text_block

ROOT = Path(__file__).resolve().parents[1]
URL = "https://openrouter.ai/api/v1/chat/completions"
# what OpenRouter returns when the upstream provider rate-limits a model
UPSTREAM = "google/gemini-3.7-flash is temporarily rate-limited upstream. Please retry shortly."
OPENROUTER_BODY = json.dumps({"error": {
    "message": "Provider returned error", "code": 429,
    "metadata": {"raw": UPSTREAM, "provider_name": "Google AI Studio"}}})
PROVIDER_MESSAGE = f"Provider returned error (Google AI Studio: {UPSTREAM})"
DEFAULT_WAITS = [10, 20, 40, 60, 60, 60]


def http_error(status: int = 429, body: str = OPENROUTER_BODY,
               retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, status, "Too Many Requests", headers,
                                  io.BytesIO(body.encode("utf-8")))


def http_429(retry_after: str | None = None, body: str = OPENROUTER_BODY) -> RuntimeError:
    """A 429 as model_api.OpenAICompatibleMessages raises it."""
    error = RuntimeError(f"Model request failed: HTTP 429: {body}")
    error.__cause__ = http_error(429, body, retry_after)
    return error


def failing(*errors):
    """A call that raises ``errors`` in turn, then returns "ok"; it counts its calls."""
    pending = list(errors)

    def call():
        call.count += 1
        if pending:
            raise pending.pop(0)
        return "ok"

    call.count = 0
    return call


class TraceStub:
    finished = False

    def __init__(self):
        self.events = []

    def event(self, event_type, **payload):
        self.events.append({"type": event_type, **payload})


@pytest.fixture
def waits(monkeypatch):
    """The waits with_retry makes, without waiting; defaults for the settings."""
    made = []
    monkeypatch.setattr(recovery, "_sleep", made.append)
    monkeypatch.setattr(recovery, "retry_delay", lambda attempt: 0.0)
    monkeypatch.setattr(recovery, "MAX_RETRIES", 3)
    monkeypatch.setattr(recovery, "CASE_DEADLINE", None)
    monkeypatch.setattr(config, "RATE_LIMIT_RETRIES", DEFAULT_RATE_LIMIT_RETRIES)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_DELAY", DEFAULT_RATE_LIMIT_MAX_DELAY)
    monkeypatch.setattr(config, "RATE_LIMIT_ERROR", "")
    return made


@pytest.fixture
def events(monkeypatch):
    stub = TraceStub()
    monkeypatch.setattr(trace, "CURRENT_TRACE", stub)
    return stub.events


def retries_in(events):
    return [event for event in events if event["type"] == "rate_limit_retry"]


# ── the settings ──


def test_the_defaults():
    assert (DEFAULT_RATE_LIMIT_RETRIES, DEFAULT_RATE_LIMIT_MAX_DELAY) == (6, 60.0)
    assert parse_rate_limit({}) == (6, 60.0, "")
    assert parse_rate_limit({RATE_LIMIT_RETRIES_ENV: "", RATE_LIMIT_MAX_DELAY_ENV: "  "}) == (
        6, 60.0, "")
    # the test session runs with both variables empty (tests/conftest.py)
    assert (config.RATE_LIMIT_RETRIES, config.RATE_LIMIT_MAX_DELAY, config.RATE_LIMIT_ERROR) == (
        6, 60.0, "")
    assert config.configured_rate_limit() == (6, 60.0)


@pytest.mark.parametrize("retries, max_delay, expected", [
    ("3", "30", (3, 30.0)),
    ("0", "1", (0, 1.0)),             # 0 turns 429 retries off
    (" 10 ", '"120"', (10, 120.0)),
    ("'2'", "7.5", (2, 7.5)),
    ("8", "", (8, 60.0)),
    ("", "15", (6, 15.0)),
])
def test_both_settings_take_effect(retries, max_delay, expected):
    environ = {RATE_LIMIT_RETRIES_ENV: retries, RATE_LIMIT_MAX_DELAY_ENV: max_delay}
    assert parse_rate_limit(environ) == (*expected, "")


@pytest.mark.parametrize("value", ["abc", "-1", "2.5", "1e3", "six", "+3", "3 retries"])
def test_retries_that_are_not_a_whole_number_are_an_error(value):
    assert parse_rate_limit({RATE_LIMIT_RETRIES_ENV: value}) == (
        6, 60.0,
        f"AQOURS_CODE_RATE_LIMIT_RETRIES must be a whole number of retries (0 or more), "
        f"got {value!r}")


@pytest.mark.parametrize("value", ["0", "0.0", "-5", "abc", "1e2", "inf", "nan", "60s", ".5"])
def test_a_maximum_wait_that_is_not_a_positive_number_is_an_error(value):
    assert parse_rate_limit({RATE_LIMIT_MAX_DELAY_ENV: value}) == (
        6, 60.0,
        f"AQOURS_CODE_RATE_LIMIT_MAX_DELAY must be a positive number of seconds, "
        f"got {value!r}")


def test_both_invalid_name_both():
    error = parse_rate_limit({RATE_LIMIT_RETRIES_ENV: "x", RATE_LIMIT_MAX_DELAY_ENV: "y"})[2]
    assert error == ("AQOURS_CODE_RATE_LIMIT_RETRIES must be a whole number of retries "
                     "(0 or more), got 'x'; AQOURS_CODE_RATE_LIMIT_MAX_DELAY must be a "
                     "positive number of seconds, got 'y'")


def test_an_invalid_setting_stops_validation_and_every_use(monkeypatch, waits):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(config, "MODEL", "some-model")
    config.validate_runtime_configuration()  # valid as it is
    error = parse_rate_limit({RATE_LIMIT_RETRIES_ENV: "many"})[2]
    monkeypatch.setattr(config, "RATE_LIMIT_ERROR", error)
    message = "AQOURS_CODE_RATE_LIMIT_RETRIES must be a whole number"
    with pytest.raises(RuntimeError, match=message):
        config.validate_runtime_configuration()
    with pytest.raises(RuntimeError, match=message):
        config.configured_rate_limit()
    # with_retry refuses before the first model call
    call = failing()
    with pytest.raises(RuntimeError, match=message):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 0


def test_an_invalid_setting_stops_the_agent_loop_before_any_model_call(monkeypatch):
    install_common_agent_mocks(monkeypatch)
    client = FakeClient([response([text_block("done")])])
    monkeypatch.setattr(agent_loop, "client", client)
    monkeypatch.setattr(config, "RATE_LIMIT_ERROR",
                        parse_rate_limit({RATE_LIMIT_MAX_DELAY_ENV: "soon"})[2])
    with pytest.raises(RuntimeError, match="AQOURS_CODE_RATE_LIMIT_MAX_DELAY must be a positive"):
        agent_loop.agent_loop([{"role": "user", "content": "hi"}], {})
    assert client.messages.calls == []


def _config_in_child(env: dict) -> list:
    """The settings as a fresh process reads them from its environment."""
    code = ("import json; from aqours_code import config as c; print(json.dumps("
            "[c.RATE_LIMIT_RETRIES, c.RATE_LIMIT_MAX_DELAY, c.RATE_LIMIT_ERROR]))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=60, env={**os.environ, **env})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_environment_variables_reach_config():
    # set (even empty), a variable is not taken from .env
    assert _config_in_child({RATE_LIMIT_RETRIES_ENV: "", RATE_LIMIT_MAX_DELAY_ENV: ""}) == [
        6, 60.0, ""]
    assert _config_in_child({RATE_LIMIT_RETRIES_ENV: "2", RATE_LIMIT_MAX_DELAY_ENV: "90"}) == [
        2, 90.0, ""]
    assert _config_in_child({RATE_LIMIT_RETRIES_ENV: "lots", RATE_LIMIT_MAX_DELAY_ENV: ""}) == [
        6, 60.0, "AQOURS_CODE_RATE_LIMIT_RETRIES must be a whole number of retries (0 or more), "
                 "got 'lots'"]


# ── reading a 429 ──


def test_retry_after_in_seconds_or_as_a_date():
    now = 1_800_000_000.0
    assert parse_retry_after("30", now) == 30.0
    assert parse_retry_after(" 1.5 ", now) == 1.5
    assert parse_retry_after("0", now) == 0.0
    assert parse_retry_after(email.utils.formatdate(now + 45, usegmt=True), now) == 45.0
    assert parse_retry_after(email.utils.formatdate(now - 45, usegmt=True), now) == 0.0
    for value in ("soon", "-5", "", "1e3"):
        assert parse_retry_after(value, now) is None


def test_retry_after_is_read_through_the_exception_chain():
    assert retry_after_seconds(http_429(retry_after="12")) == 12.0
    assert retry_after_seconds(http_429()) is None
    # an SDK error (anthropic) keeps the response, with its headers
    sdk_error = type("RateLimitError", (Exception,), {})("rate limited")
    sdk_error.status_code = 429
    sdk_error.response = SimpleNamespace(headers={"retry-after": "7"})
    assert retry_after_seconds(sdk_error) == 7.0
    assert is_rate_limit_error(sdk_error)


def test_what_counts_as_a_429():
    assert is_rate_limit_error(http_429())
    assert is_rate_limit_error(RuntimeError("Model request failed: HTTP 429: slow down"))
    assert is_rate_limit_error(type("RateLimitError", (Exception,), {})("slow down"))
    # another status whose body mentions 429 is that status
    assert not is_rate_limit_error(RuntimeError("Model request failed: HTTP 500: code 429"))
    assert not is_rate_limit_error(TimeoutError("request a429b timed out"))


def test_the_provider_message_is_taken_from_the_body():
    assert provider_message(http_429()) == PROVIDER_MESSAGE
    plain = json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-min"}})
    assert provider_message(http_429(body=plain)) == "Rate limit exceeded: free-models-per-min"
    assert provider_message(RuntimeError("Model request failed: HTTP 429: slow down")) == (
        "Model request failed: HTTP 429: slow down")


def test_the_wait_without_retry_after_doubles_up_to_the_maximum():
    middle = SimpleNamespace(uniform=lambda low, high: (low + high) / 2)
    assert [rate_limit_wait(http_429(), n, 60, rng=middle).delay_s for n in range(1, 8)] == [
        10, 20, 40, 60, 60, 60, 60]
    assert [rate_limit_wait(http_429(), n, 30, rng=middle).delay_s for n in range(1, 5)] == [
        10, 20, 30, 30]
    # jitter: +-10%, never above the maximum
    rng = random.Random(7)
    for n, base in enumerate(DEFAULT_WAITS, start=1):
        for _ in range(50):
            delay = rate_limit_wait(http_429(), n, 60, rng=rng).delay_s
            assert base * 0.9 <= delay <= min(base * 1.1, 60)


def test_the_wait_follows_retry_after_up_to_the_maximum():
    wait = rate_limit_wait(http_429(retry_after="25"), 1, 60)
    assert (wait.delay_s, wait.retry_after, wait.retry_after_s, wait.capped) == (25, True, 25, False)
    wait = rate_limit_wait(http_429(retry_after="300"), 1, 60)
    assert (wait.delay_s, wait.retry_after, wait.retry_after_s, wait.capped) == (60, True, 300, True)
    assert wait.record(2, 6) == {"retry": 2, "max_retries": 6, "delay_s": 60.0,
                                 "retry_after": True, "retry_after_s": 300.0, "capped": True}


# ── with_retry ──


def test_with_retry_after_it_waits_what_the_provider_asks(waits, events):
    call = failing(http_429(retry_after="7"), http_429(retry_after="3"))
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    assert call.count == 3
    assert waits == [7.0, 3.0]
    assert retries_in(events) == [
        {"type": "rate_limit_retry", "retry": 1, "max_retries": 6, "delay_s": 7.0,
         "retry_after": True, "retry_after_s": 7.0, "capped": False, "source": "direct"},
        {"type": "rate_limit_retry", "retry": 2, "max_retries": 6, "delay_s": 3.0,
         "retry_after": True, "retry_after_s": 3.0, "capped": False, "source": "direct"},
    ]


def test_with_retry_after_a_date(waits):
    when = email.utils.formatdate(time.time() + 30, usegmt=True)
    call = failing(http_429(retry_after=when))
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    assert 28 <= waits[0] <= 30


def test_a_retry_after_over_the_maximum_waits_the_maximum(monkeypatch, waits, events):
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_DELAY", 45.0)
    call = failing(http_429(retry_after="600"))
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    assert waits == [45.0]
    assert retries_in(events)[0]["capped"] is True
    assert retries_in(events)[0]["retry_after_s"] == 600.0


def test_without_retry_after_it_waits_10_20_40_60_60_60(monkeypatch, waits, events):
    monkeypatch.setattr(random, "uniform", lambda low, high: (low + high) / 2)
    call = failing(*(http_429() for _ in range(6)))
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    assert call.count == 7
    assert waits == DEFAULT_WAITS
    recorded = retries_in(events)
    assert [event["retry"] for event in recorded] == [1, 2, 3, 4, 5, 6]
    assert [event["delay_s"] for event in recorded] == DEFAULT_WAITS
    assert {event["retry_after"] for event in recorded} == {False}
    assert {event["retry_after_s"] for event in recorded} == {None}


def test_the_waits_have_a_little_jitter(waits):
    call = failing(*(http_429() for _ in range(6)))
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    for delay, base in zip(waits, DEFAULT_WAITS):
        assert base * 0.9 <= delay <= min(base * 1.1, 60)


def test_429_retries_do_not_use_up_the_other_retries(waits):
    # MAX_RETRIES is 3: the old with_retry stopped at the third 429
    call = failing(*(http_429() for _ in range(5)))
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    assert call.count == 6


def test_when_the_retries_run_out_the_error_keeps_the_last_429(waits, events):
    errors = [http_429() for _ in range(7)]
    call = failing(*errors)
    with pytest.raises(RateLimitRetriesExhausted) as caught:
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 7  # the first call and 6 retries
    assert len(waits) == len(retries_in(events)) == 6
    message = str(caught.value)
    assert message.startswith(
        "HTTP 429 rate limit: gave up after 6 retries (AQOURS_CODE_RATE_LIMIT_RETRIES=6); "
        f"provider message: {PROVIDER_MESSAGE}; last error: RuntimeError: "
        "Model request failed: HTTP 429: ")
    assert UPSTREAM in message
    assert "Max retries" not in message
    assert isinstance(caught.value, RuntimeError)
    assert caught.value.__cause__ is errors[-1]
    assert (caught.value.status, caught.value.retries, caught.value.reason) == (
        429, 6, "retry_limit")
    assert caught.value.provider_message == PROVIDER_MESSAGE


def test_the_settings_change_the_count_and_the_longest_wait(monkeypatch, waits):
    monkeypatch.setattr(config, "RATE_LIMIT_RETRIES", 2)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_DELAY", 15.0)
    monkeypatch.setattr(random, "uniform", lambda low, high: (low + high) / 2)
    call = failing(*(http_429() for _ in range(3)))
    with pytest.raises(RateLimitRetriesExhausted,
                       match=r"gave up after 2 retries \(AQOURS_CODE_RATE_LIMIT_RETRIES=2\)"):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 3
    assert waits == [10.0, 15.0]


def test_no_429_retry_when_they_are_turned_off(monkeypatch, waits):
    monkeypatch.setattr(config, "RATE_LIMIT_RETRIES", 0)
    call = failing(http_429())
    with pytest.raises(RateLimitRetriesExhausted, match="gave up after 0 retries"):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 1
    assert waits == []


def test_no_429_retry_whose_wait_passes_the_deadline(monkeypatch, waits, events):
    monkeypatch.setattr(recovery, "CASE_DEADLINE", time.monotonic() + 15)
    # the first wait (about 10 s) fits, the second (about 20 s) does not
    call = failing(http_429(), http_429())
    with pytest.raises(RateLimitRetriesExhausted) as caught:
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 2
    assert len(waits) == len(retries_in(events)) == 1
    assert str(caught.value).startswith(
        "HTTP 429 rate limit: gave up after 1 retries: the next wait would pass the run's "
        f"deadline; provider message: {PROVIDER_MESSAGE}")
    assert caught.value.reason == "deadline"


def test_a_retry_after_past_the_deadline_is_not_waited(monkeypatch, waits):
    monkeypatch.setattr(recovery, "CASE_DEADLINE", time.monotonic() + 5)
    call = failing(http_429(retry_after="30"))
    with pytest.raises(RateLimitRetriesExhausted, match="would pass the run's deadline"):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 1
    assert waits == []


def test_the_given_up_error_is_not_retried_again_by_an_outer_with_retry(waits):
    inner = failing(*(http_429() for _ in range(7)))
    outer = failing()

    def nested():
        outer.count += 1
        return recovery.with_retry(inner, recovery.RecoveryState())

    with pytest.raises(RateLimitRetriesExhausted):
        recovery.with_retry(nested, recovery.RecoveryState())
    assert (outer.count, inner.count) == (1, 7)


def test_529_and_other_errors_are_unchanged(waits):
    overloaded = [RuntimeError("Model request failed: HTTP 529: overloaded") for _ in range(3)]
    call = failing(*overloaded)
    with pytest.raises(RuntimeError, match=r"^Max retries \(3\) exceeded$"):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 3
    # a 500 whose body mentions 429 follows the 5xx rule: the last attempt raises it
    server = [RuntimeError("Model request failed: HTTP 500: upstream code 429") for _ in range(3)]
    call = failing(*server)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 3
    assert waits == [0.0] * 5  # retry_delay (patched to 0), not the 429 waits


def test_a_429_from_the_real_openrouter_client(monkeypatch, waits, events):
    """The OpenAI-compatible client, with urlopen answering 429 twice."""
    ok = json.dumps({"choices": [{"message": {"role": "assistant", "content": "done"},
                                  "finish_reason": "stop"}]}).encode("utf-8")

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return ok

    answers = [http_error(retry_after="4"), http_error(), Reply()]

    def fake_urlopen(request, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    messages = OpenAICompatibleMessages("test-key-not-real", "https://openrouter.ai/api/v1")
    result = recovery.with_retry(
        lambda: messages.create(model="google/gemini-3.7-flash", messages=[
            {"role": "user", "content": "plan"}]),
        recovery.RecoveryState())
    assert result.content[0].text == "done"
    # the second retry has no Retry-After: the second wait of the schedule (20 s)
    assert waits[0] == 4.0 and 18 <= waits[1] <= 22
    assert [(event["retry"], event["retry_after"]) for event in retries_in(events)] == [
        (1, True), (2, False)]


def test_the_retries_are_written_to_trace_jsonl(tmp_path, waits):
    run = trace.start_run("plan", workdir=tmp_path, model_provider="openai_compatible",
                          model="google/gemini-3.7-flash", storage_root=tmp_path)
    try:
        call = failing(http_429(retry_after="2"), http_429())
        assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    finally:
        trace.finish_run("done")
    lines = Path(run.trace_path).read_text(encoding="utf-8").splitlines()
    recorded = [event for event in map(json.loads, lines) if event["type"] == "rate_limit_retry"]
    assert [(event["retry"], event["retry_after"], event["source"]) for event in recorded] == [
        (1, True, "direct"), (2, False, "direct")]
    assert recorded[0]["delay_s"] == 2.0
    assert recorded[0]["retry_after_s"] == 2.0 and recorded[1]["retry_after_s"] is None


# ── the Docker model Broker ──


class RateLimitedMessages:
    """Answers 429 (``errors``) and then text; counts the calls it gets."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="served")],
                               stop_reason="end_turn")


def start_broker(tmp_path, messages, **settings):
    nonce = uuid.uuid4().hex
    options = {"allowed_model": "case-model", "max_calls": 16, "provider_timeout": 0.5,
               "max_provider_retries": 1, "provider_retry_delay": 0, "delivery_grace": 0.2,
               "rate_limit_retries": 6, "rate_limit_max_delay": 0.01, **settings}
    broker = ModelBroker(tmp_path, nonce, SimpleNamespace(messages=messages), **options).start()
    client = BrokerModelClient(tmp_path, nonce, request_timeout=10)
    return broker, client


def call_through_broker(client):
    return client.messages.create(model="case-model", messages=[], max_tokens=1000)


def test_the_broker_retries_429_by_the_same_rules(tmp_path, waits, events):
    # max_provider_retries=1 would have allowed only one retry before
    messages = RateLimitedMessages([http_429(retry_after="0"), http_429(), http_429()])
    broker, client = start_broker(tmp_path, messages)
    logical_calls = 0

    def call():
        nonlocal logical_calls
        logical_calls += 1
        return call_through_broker(client)

    try:
        result = recovery.with_retry(call, recovery.RecoveryState())
    finally:
        broker.stop()
    assert result.content[0].text == "served"
    assert (logical_calls, messages.calls) == (1, 4)  # with_retry did not retry again
    assert (broker.rate_limit_retry_count, broker.retry_count, broker.call_count) == (3, 3, 4)
    assert waits == []  # the container waited for nothing itself
    recorded = retries_in(events)
    assert [(event["retry"], event["retry_after"], event["source"]) for event in recorded] == [
        (1, True, "broker"), (2, False, "broker"), (3, False, "broker")]
    assert {event["max_retries"] for event in recorded} == {6}
    assert all(0 <= event["delay_s"] <= 0.01 for event in recorded)
    stats = json.loads((tmp_path / "stats" / "broker_stats.json").read_text(encoding="utf-8"))
    assert (stats["rate_limit_retry_count"], stats["max_rate_limit_retries"],
            stats["rate_limit_max_delay"]) == (3, 6, 0.01)


def test_when_the_broker_gives_up_the_error_keeps_the_last_429(tmp_path, waits, events):
    messages = RateLimitedMessages([http_429() for _ in range(10)])
    broker, client = start_broker(tmp_path, messages, rate_limit_retries=2)
    logical_calls = 0

    def call():
        nonlocal logical_calls
        logical_calls += 1
        return call_through_broker(client)

    try:
        with pytest.raises(BrokerRemoteError) as caught:
            recovery.with_retry(call, recovery.RecoveryState())
    finally:
        broker.stop()
    assert (logical_calls, messages.calls) == (1, 3)
    assert caught.value.error_kind == "provider_http_429"
    message = str(caught.value)
    assert message.startswith(
        "provider_http_429: RateLimitRetriesExhausted: HTTP 429 rate limit: gave up after "
        "2 retries (AQOURS_CODE_RATE_LIMIT_RETRIES=2); "
        f"provider message: {PROVIDER_MESSAGE}; last error: RuntimeError: "
        "Model request failed: HTTP 429: ")
    assert [event["retry"] for event in retries_in(events)] == [1, 2]
    assert broker.last_error_kind == "provider_http_429"


def test_the_broker_does_not_start_a_429_retry_past_the_case_deadline(tmp_path):
    messages = RateLimitedMessages([http_429(retry_after="30") for _ in range(3)])
    broker, client = start_broker(tmp_path, messages, rate_limit_max_delay=60,
                                  case_deadline=time.monotonic() + 5)
    try:
        with pytest.raises(BrokerRemoteError) as caught:
            call_through_broker(client)
    finally:
        broker.stop()
    assert messages.calls == 1
    assert broker.retry_skipped_reason == "case_deadline"
    assert "gave up after 0 retries: the next wait would pass the case deadline" in str(
        caught.value)


def test_other_transient_errors_keep_the_broker_rule(tmp_path):
    timeout = urllib.error.URLError(TimeoutError("handshake timed out"))
    messages = RateLimitedMessages([timeout, timeout])
    broker, client = start_broker(tmp_path, messages)
    try:
        with pytest.raises(BrokerRemoteError) as caught:
            call_through_broker(client)
    finally:
        broker.stop()
    assert caught.value.error_kind == "provider_timeout"
    assert messages.calls == 2  # one retry (max_provider_retries=1), as before
    assert (broker.retry_count, broker.rate_limit_retry_count) == (1, 0)


def test_the_broker_reads_the_settings_and_refuses_invalid_ones(tmp_path, monkeypatch):
    monkeypatch.setenv(RATE_LIMIT_RETRIES_ENV, "4")
    monkeypatch.setenv(RATE_LIMIT_MAX_DELAY_ENV, "30")
    broker = ModelBroker(tmp_path, uuid.uuid4().hex, SimpleNamespace(messages=None),
                         allowed_model="case-model", provider_timeout=1)
    assert (broker.rate_limit_retries, broker.rate_limit_max_delay) == (4, 30.0)
    monkeypatch.setenv(RATE_LIMIT_MAX_DELAY_ENV, "a minute")
    with pytest.raises(ValueError, match="AQOURS_CODE_RATE_LIMIT_MAX_DELAY must be a positive"):
        ModelBroker(tmp_path, uuid.uuid4().hex, SimpleNamespace(messages=None),
                    allowed_model="case-model", provider_timeout=1)


def test_the_container_waits_through_the_broker_429_retries():
    without = broker_ipc_wait_timeout(30, max_provider_retries=1, retry_delay=1, delivery_grace=5)
    assert without == 66
    # 6 more attempts of at most 30 s, each after a wait of at most 60 s
    assert broker_ipc_wait_timeout(30, max_provider_retries=1, retry_delay=1, delivery_grace=5,
                                   rate_limit_retries=6, rate_limit_max_delay=60) == 66 + 6 * 90
