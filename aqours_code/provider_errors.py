"""Classify a model provider error, and tell whether it is worth retrying.

Shared by the Docker model broker (``model_broker``) and the direct-call retry
(``recovery.with_retry``), so both retry the same failures: a timeout, a reset
or refused connection, a TLS error, and HTTP 408, 429, 500, 502, 503, 504, 529.

HTTP 429 (rate limit) has rules of its own, the same in both: its retries are
counted apart from the others' (AQOURS_CODE_RATE_LIMIT_RETRIES, default 6),
and each waits what the provider's Retry-After says, else 10 s, 20 s, 40 s,
then 60 s with a little jitter, never more than
AQOURS_CODE_RATE_LIMIT_MAX_DELAY (default 60 s) at a time
(:func:`rate_limit_wait`). When they run out, :class:`RateLimitRetriesExhausted`
keeps the provider's last message.
"""
from __future__ import annotations

import email.utils
import http.client
import json
import random
import re
import ssl
import time
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timezone

TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 529}
RATE_LIMIT_STATUS = 429


def exception_chain(exc: BaseException):
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for candidate in (
            getattr(current, "reason", None),
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(candidate, BaseException):
                pending.append(candidate)


def provider_error_kind(exc: BaseException) -> str:
    chain = list(exception_chain(exc))
    combined = " ".join(str(item).lower() for item in chain)
    for item in chain:
        if isinstance(item, urllib.error.HTTPError):
            return f"provider_http_{item.code}"
        status_code = getattr(item, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(item, "response", None), "status_code", None)
        try:
            if status_code is not None:
                return f"provider_http_{int(status_code)}"
        except (TypeError, ValueError):
            pass
    match = re.search(r"\bhttp\s+(\d{3})\b", combined)
    if match:
        return f"provider_http_{match.group(1)}"
    if (any(isinstance(item, TimeoutError) for item in chain)
            or "timed out" in combined or "timeout" in combined):
        return "provider_timeout"
    if (any(isinstance(item, (ConnectionError, http.client.IncompleteRead))
            for item in chain)
            or "incompleteread" in combined
            or "incomplete read" in combined
            or "connection reset" in combined
            or "connection refused" in combined
            or "temporarily unavailable" in combined):
        return "provider_connection_error"
    if (any(isinstance(item, ssl.SSLError) for item in chain)
            or "ssl" in combined or "tls" in combined):
        return "provider_tls_error"
    if any(isinstance(item, urllib.error.URLError) for item in chain):
        return "provider_connection_error"
    return "provider_error"


def is_transient_provider_error(error_kind: str) -> bool:
    if error_kind in {
        "provider_timeout", "provider_connection_error", "provider_tls_error",
    }:
        return True
    if error_kind.startswith("provider_http_"):
        try:
            return int(error_kind.rsplit("_", 1)[1]) in TRANSIENT_HTTP_STATUSES
        except ValueError:
            return False
    return False


# ── HTTP 429 (rate limit) ──

RATE_LIMIT_RETRIES_ENV = "AQOURS_CODE_RATE_LIMIT_RETRIES"
RATE_LIMIT_MAX_DELAY_ENV = "AQOURS_CODE_RATE_LIMIT_MAX_DELAY"
DEFAULT_RATE_LIMIT_RETRIES = 6
DEFAULT_RATE_LIMIT_MAX_DELAY = 60.0
# Without Retry-After, retry n waits FIRST * 2**(n-1), at most the maximum
# wait: 10, 20, 40, 60, 60, 60 s with the defaults.
RATE_LIMIT_FIRST_DELAY = 10.0
RATE_LIMIT_JITTER = 0.1  # +-10% of such a wait, so parallel callers spread out
_WHOLE_NUMBER = re.compile(r"[0-9]+")
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _unquoted(raw) -> str:
    raw = str(raw or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    return raw


def parse_rate_limit(environ: Mapping[str, str]) -> tuple[int, float, str]:
    """``(retries, max_delay, error)`` from AQOURS_CODE_RATE_LIMIT_RETRIES and
    AQOURS_CODE_RATE_LIMIT_MAX_DELAY in ``environ``.

    An unset or empty variable takes its default (6 and 60). The retries must
    be a whole number (0 turns 429 retries off), the maximum wait a positive
    number of seconds. Otherwise ``error`` says what is wrong, and both are
    the defaults (``config.configured_rate_limit`` then refuses to give them).
    """
    errors = []
    retries, max_delay = DEFAULT_RATE_LIMIT_RETRIES, DEFAULT_RATE_LIMIT_MAX_DELAY
    raw = _unquoted(environ.get(RATE_LIMIT_RETRIES_ENV))
    if raw:
        if _WHOLE_NUMBER.fullmatch(raw):
            retries = int(raw)
        else:
            errors.append(f"{RATE_LIMIT_RETRIES_ENV} must be a whole number of retries "
                          f"(0 or more), got {raw!r}")
    raw = _unquoted(environ.get(RATE_LIMIT_MAX_DELAY_ENV))
    if raw:
        if _SECONDS.fullmatch(raw) and float(raw) > 0:
            max_delay = float(raw)
        else:
            errors.append(f"{RATE_LIMIT_MAX_DELAY_ENV} must be a positive number of "
                          f"seconds, got {raw!r}")
    if errors:
        return DEFAULT_RATE_LIMIT_RETRIES, DEFAULT_RATE_LIMIT_MAX_DELAY, "; ".join(errors)
    return retries, max_delay, ""


def is_rate_limit_error(exc: BaseException, error_kind: str | None = None) -> bool:
    """Whether ``exc`` is an HTTP 429 (rate limit)."""
    error_kind = error_kind or provider_error_kind(exc)
    if error_kind == f"provider_http_{RATE_LIMIT_STATUS}":
        return True
    if error_kind.startswith("provider_http_"):
        return False  # another status, even if its text mentions 429
    return ("ratelimit" in type(exc).__name__.lower()
            or re.search(rf"\b{RATE_LIMIT_STATUS}\b", str(exc)) is not None)


def _header(headers, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for key in (name, name.lower()):
        try:
            value = getter(key)
        except Exception:  # noqa: BLE001 - a header object of an unknown kind
            value = None
        if value is not None:
            return str(value)
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == name.lower() and value is not None:
                return str(value)
    return None


def parse_retry_after(value: str, now: float | None = None) -> float | None:
    """The seconds a Retry-After value asks to wait, or None if it is not one.

    It is a number of seconds (``"30"``) or an HTTP date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``); a date already past is 0.
    """
    value = str(value).strip()
    if _SECONDS.fullmatch(value):
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


def retry_after_seconds(exc: BaseException, now: float | None = None) -> float | None:
    """The Retry-After of the HTTP response behind ``exc``, in seconds, or None.

    The header is read from a ``urllib.error.HTTPError`` in the exception's
    chain (the OpenAI-compatible client raises its error from one) or from the
    ``response`` of an SDK error (anthropic).
    """
    for item in exception_chain(exc):
        for headers in (getattr(item, "headers", None),
                        getattr(getattr(item, "response", None), "headers", None)):
            value = _header(headers, "Retry-After")
            if value is not None:
                return parse_retry_after(value, now)
    return None


@dataclass(frozen=True)
class RateLimitWait:
    """How long to wait before one retry after HTTP 429, and why."""

    delay_s: float
    retry_after_s: float | None  # what Retry-After asked for, None without one
    capped: bool  # Retry-After asked for more than the maximum wait

    @property
    def retry_after(self) -> bool:
        return self.retry_after_s is not None

    def record(self, retry: int, max_retries: int) -> dict:
        """The trace fields of retry number ``retry`` (1 is the first)."""
        return {
            "retry": int(retry),
            "max_retries": int(max_retries),
            "delay_s": round(self.delay_s, 3),
            "retry_after": self.retry_after,
            "retry_after_s": (None if self.retry_after_s is None
                              else round(self.retry_after_s, 3)),
            "capped": self.capped,
        }


def rate_limit_wait(exc: BaseException, retry: int, max_delay: float, *,
                    now: float | None = None, rng=random) -> RateLimitWait:
    """The wait before 429 retry number ``retry`` (1 is the first).

    Retry-After when the response has one, else ``RATE_LIMIT_FIRST_DELAY``
    doubled at each retry with +-10% jitter; never more than ``max_delay``.
    """
    retry_after = retry_after_seconds(exc, now=now)
    if retry_after is not None:
        return RateLimitWait(min(retry_after, max_delay), retry_after, retry_after > max_delay)
    base = min(RATE_LIMIT_FIRST_DELAY * 2 ** min(max(0, retry - 1), 30), max_delay)
    jittered = base * rng.uniform(1 - RATE_LIMIT_JITTER, 1 + RATE_LIMIT_JITTER)
    return RateLimitWait(min(jittered, max_delay), None, False)


def _body_message(body) -> str | None:
    """``error.message`` of a provider's JSON error body, with OpenRouter's
    upstream provider and its raw message when there are some."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    if isinstance(error, str):
        return error
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return None
    message = error["message"]
    metadata = error.get("metadata")
    if isinstance(metadata, dict):
        upstream = metadata.get("provider_name")
        raw = metadata.get("raw")
        if isinstance(raw, str) and raw.strip() and raw.strip() not in message:
            message += f" ({upstream or 'upstream'}: {raw.strip()})"
        elif isinstance(upstream, str) and upstream:
            message += f" ({upstream})"
    return message


def provider_message(exc: BaseException, limit: int = 1000) -> str:
    """The message the provider gave with ``exc``: the ``error.message`` of its
    JSON body when one can be read, else the error's own text."""
    message = None
    for item in exception_chain(exc):
        message = _body_message(getattr(item, "body", None))
        if message:
            break
        text = str(item)
        # model_api: "Model request failed: HTTP 429: <response body>"
        match = re.search(r"\bHTTP \d{3}: ", text)
        if match:
            message = _body_message(text[match.end():].strip())
            if message:
                break
    message = message or str(exc) or type(exc).__name__
    message = " ".join(message.split())
    return message if len(message) <= limit else message[:limit - 3] + "..."


GIVE_UP_REASONS = {
    "retry_limit": "after {retries} retries ({env}={max_retries})",
    "deadline": "after {retries} retries: the next wait would pass the run's deadline",
    "call_budget": "after {retries} retries: the Broker's call budget is used up",
    "case_deadline": "after {retries} retries: the next wait would pass the case deadline",
    "broker_stopping": "after {retries} retries: the Broker is stopping",
}


class RateLimitRetriesExhausted(RuntimeError):
    """HTTP 429 that the 429 retries did not get past.

    Its message keeps the status, the provider's message and the last error,
    for example ``HTTP 429 rate limit: gave up after 6 retries
    (AQOURS_CODE_RATE_LIMIT_RETRIES=6); provider message: Rate limit exceeded;
    last error: RuntimeError: Model request failed: HTTP 429: {...}``.
    """

    # The retries are already done: an outer with_retry raises it at once
    # (the same marker the Docker Broker's errors carry).
    retry_managed = True
    status = RATE_LIMIT_STATUS

    def __init__(self, last_error: BaseException, *, retries: int, max_retries: int,
                 reason: str = "retry_limit"):
        self.last_error = last_error
        self.retries = int(retries)
        self.max_retries = int(max_retries)
        self.reason = reason
        self.provider_message = provider_message(last_error)
        why = GIVE_UP_REASONS.get(reason, "after {retries} retries (" + reason + ")").format(
            retries=self.retries, env=RATE_LIMIT_RETRIES_ENV, max_retries=self.max_retries)
        last = " ".join(f"{type(last_error).__name__}: {last_error}".split())
        if len(last) > 2000:
            last = last[:1997] + "..."
        super().__init__(f"HTTP {RATE_LIMIT_STATUS} rate limit: gave up {why}; "
                         f"provider message: {self.provider_message}; last error: {last}")
