"""with_retry: direct model calls retry the transient provider errors the Broker retries."""
from __future__ import annotations

import ssl
import time
import urllib.error

import pytest

from aqours_code import recovery
from aqours_code.model_broker import BrokerRemoteError


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(recovery, "retry_delay", lambda attempt: 0.0)
    monkeypatch.setattr(recovery, "MAX_RETRIES", 3)
    monkeypatch.setattr(recovery, "CASE_DEADLINE", None)


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


def tls_handshake_timeout():
    # what urllib raised in the dask planning run
    return urllib.error.URLError(
        TimeoutError("_ssl.c:989: The handshake operation timed out"))


TRANSIENT = [
    pytest.param(tls_handshake_timeout, id="tls-handshake-timeout"),
    pytest.param(lambda: urllib.error.URLError(ssl.SSLError("EOF occurred in violation")),
                 id="tls-error"),
    pytest.param(lambda: ConnectionResetError(10054, "An existing connection was forcibly "
                                                     "closed by the remote host"),
                 id="connection-reset"),
    pytest.param(lambda: urllib.error.URLError(ConnectionResetError(104, "reset")),
                 id="connection-reset-in-urlerror"),
    pytest.param(lambda: TimeoutError("The read operation timed out"), id="read-timeout"),
    pytest.param(lambda: RuntimeError("Model request failed: HTTP 503: busy"), id="http-503"),
]


@pytest.mark.parametrize("make", TRANSIENT)
def test_a_transient_error_is_retried_and_then_succeeds(make):
    call = failing(make(), make())
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    assert call.count == 3


@pytest.mark.parametrize("error", [
    ValueError("invalid DeepSeek thinking configuration"),
    RuntimeError("Model request failed: HTTP 400: bad request"),
    RuntimeError("Model request failed: HTTP 401: unauthorized"),
    KeyError("choices"),
])
def test_a_non_transient_error_is_raised_at_once(error):
    call = failing(error)
    with pytest.raises(type(error)):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 1


def test_max_retries_is_respected_and_the_last_error_is_raised():
    errors = [tls_handshake_timeout() for _ in range(5)]
    call = failing(*errors)
    with pytest.raises(urllib.error.URLError, match="handshake operation timed out"):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == recovery.MAX_RETRIES == 3


def test_max_retries_from_the_configuration(monkeypatch):
    monkeypatch.setattr(recovery, "MAX_RETRIES", 1)
    call = failing(ConnectionResetError("reset"), ConnectionResetError("reset"))
    with pytest.raises(ConnectionResetError):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 1


def test_no_retry_starts_after_the_deadline(monkeypatch):
    monkeypatch.setattr(recovery, "CASE_DEADLINE", time.monotonic() - 1)
    call = failing(TimeoutError("The read operation timed out"))
    with pytest.raises(TimeoutError):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 1


def test_a_broker_managed_transient_error_is_not_retried_again():
    call = failing(BrokerRemoteError("provider_timeout", "timed out", "request_1234567890"))
    with pytest.raises(BrokerRemoteError):
        recovery.with_retry(call, recovery.RecoveryState())
    assert call.count == 1


def test_rate_limits_are_retried_as_before():
    call = failing(RuntimeError("Model request failed: HTTP 429: slow down"))
    assert recovery.with_retry(call, recovery.RecoveryState()) == "ok"
    assert call.count == 2
