"""Classify a model provider error, and tell whether it is worth retrying.

Shared by the Docker model broker (``model_broker``) and the direct-call retry
(``recovery.with_retry``), so both retry the same failures: a timeout, a reset
or refused connection, a TLS error, and HTTP 408, 429, 500, 502, 503, 504, 529.
"""
from __future__ import annotations

import http.client
import re
import ssl
import urllib.error

TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 529}


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
