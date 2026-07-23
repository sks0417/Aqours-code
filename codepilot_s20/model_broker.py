from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import uuid
from pathlib import Path
from types import SimpleNamespace


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_TOKENS_PER_CALL = 16000
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ALLOWED_CREATE_KEYS = {"model", "system", "messages", "tools", "max_tokens"}
DEFAULT_PROVIDER_RETRIES = 1
DEFAULT_PROVIDER_RETRY_DELAY = 1.0
DEFAULT_IPC_DELIVERY_GRACE = 5.0
_TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 529}


class BrokerProtocolError(RuntimeError):
    pass


class BrokerIpcTimeout(TimeoutError):
    """The container did not receive a Broker response in the full window."""

    retry_managed = True

    def __init__(self, request_id: str):
        self.request_id = request_id
        super().__init__(
            "broker_ipc_timeout: model broker did not deliver a response "
            f"before its deadline (request_id={request_id})")


class BrokerRemoteError(RuntimeError):
    """A structured Host Broker/provider failure delivered to the container."""

    retry_managed = True

    def __init__(self, error_kind: str, message: str, request_id: str):
        self.error_kind = error_kind or "broker_remote_error"
        self.request_id = request_id
        super().__init__(
            f"{self.error_kind}: {message} (request_id={request_id})")


class _ProviderCallError(RuntimeError):
    def __init__(self, error_kind: str, original: BaseException):
        self.error_kind = error_kind
        self.original = original
        super().__init__(f"{type(original).__name__}: {original}")


def broker_ipc_wait_timeout(
    provider_timeout: float,
    *,
    max_provider_retries: int = DEFAULT_PROVIDER_RETRIES,
    retry_delay: float = DEFAULT_PROVIDER_RETRY_DELAY,
    delivery_grace: float = DEFAULT_IPC_DELIVERY_GRACE,
) -> float:
    """Cover all Provider attempts, retry delays, and final IPC delivery."""
    provider_timeout = max(0.1, float(provider_timeout))
    retries = max(0, int(max_provider_retries))
    return (
        provider_timeout * (retries + 1)
        + max(0.0, float(retry_delay)) * retries
        + max(0.0, float(delivery_grace))
    )


def _exception_chain(exc: BaseException):
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


def _provider_error_kind(exc: BaseException) -> str:
    chain = list(_exception_chain(exc))
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
    if (any(isinstance(item, ConnectionError) for item in chain)
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


def _is_transient_provider_error(error_kind: str) -> bool:
    if error_kind in {
        "provider_timeout", "provider_connection_error", "provider_tls_error",
    }:
        return True
    if error_kind.startswith("provider_http_"):
        try:
            return int(error_kind.rsplit("_", 1)[1]) in _TRANSIENT_HTTP_STATUSES
        except ValueError:
            return False
    return False


def _validate_token(value: str, label: str) -> str:
    value = str(value)
    if not _TOKEN_RE.fullmatch(value):
        raise BrokerProtocolError(f"invalid broker {label}")
    return value


def _message_value(value):
    if isinstance(value, dict):
        return {str(key): _message_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_message_value(item) for item in value]
    if hasattr(value, "type"):
        kind = getattr(value, "type", None)
        if kind == "text":
            return {"type": "text", "text": str(getattr(value, "text", ""))}
        if kind == "tool_use":
            return {
                "type": "tool_use",
                "id": str(getattr(value, "id", "")),
                "name": str(getattr(value, "name", "")),
                "input": _message_value(getattr(value, "input", {})),
            }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _usage_field(usage, *names: str) -> int:
    for name in names:
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if isinstance(value, bool):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _response_usage(response) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}

    input_tokens = _usage_field(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_field(usage, "output_tokens", "completion_tokens")
    cache_creation_tokens = _usage_field(
        usage, "cache_creation_input_tokens", "cache_write_tokens")
    cache_read_tokens = _usage_field(
        usage, "cache_read_input_tokens", "cached_input_tokens")
    prompt_details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage, dict)
        else getattr(usage, "prompt_tokens_details", None)
    )
    if not cache_read_tokens and prompt_details is not None:
        cache_read_tokens = _usage_field(prompt_details, "cached_tokens")
    explicit_total = _usage_field(usage, "total_tokens")
    total_tokens = explicit_total or sum((
        input_tokens,
        output_tokens,
        cache_creation_tokens,
        cache_read_tokens,
    ))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_tokens,
        "cache_read_input_tokens": cache_read_tokens,
        "total_tokens": total_tokens,
    }


def _response_payload(response) -> dict:
    payload = {
        "content": _message_value(getattr(response, "content", [])),
        "stop_reason": getattr(response, "stop_reason", None),
    }
    usage = _response_usage(response)
    if usage:
        payload["usage"] = usage
    return payload


def _response_object(payload: dict):
    if not isinstance(payload, dict):
        raise BrokerProtocolError("invalid broker response payload")
    blocks = []
    for block in payload.get("content", []):
        if not isinstance(block, dict):
            raise BrokerProtocolError("invalid broker content block")
        kind = block.get("type")
        if kind == "text":
            blocks.append(SimpleNamespace(type="text", text=str(block.get("text", ""))))
        elif kind == "tool_use":
            blocks.append(SimpleNamespace(
                type="tool_use",
                id=str(block.get("id", "")),
                name=str(block.get("name", "")),
                input=block.get("input") if isinstance(block.get("input"), dict) else {},
            ))
        else:
            raise BrokerProtocolError(f"unsupported broker content block: {kind}")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return SimpleNamespace(
        content=blocks,
        stop_reason=payload.get("stop_reason") or "end_turn",
        usage=SimpleNamespace(**usage),
    )


def _atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _protocol_path(root: Path, directory: str, nonce: str, request_id: str) -> Path:
    nonce = _validate_token(nonce, "nonce")
    request_id = _validate_token(request_id, "request id")
    base = (root / directory).resolve()
    path = (base / f"{nonce}-{request_id}.json").resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise BrokerProtocolError("broker path escapes IPC directory") from exc
    return path


def _validate_request(
    payload: dict,
    *,
    nonce: str,
    request_id: str,
    allowed_model: str,
    max_tokens_per_call: int,
) -> dict:
    if not isinstance(payload, dict):
        raise BrokerProtocolError("broker request must be an object")
    expected = {
        "version": PROTOCOL_VERSION,
        "nonce": nonce,
        "request_id": request_id,
        "method": "messages.create",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise BrokerProtocolError(f"invalid broker request field: {key}")
    params = payload.get("params")
    if not isinstance(params, dict) or set(params) - _ALLOWED_CREATE_KEYS:
        raise BrokerProtocolError("broker request contains unsupported parameters")
    if not isinstance(params.get("messages"), list):
        raise BrokerProtocolError("broker messages must be a list")
    if not isinstance(params.get("model"), str) or not params["model"]:
        raise BrokerProtocolError("broker model must be a non-empty string")
    if params["model"] != allowed_model:
        raise BrokerProtocolError(
            f"broker model is not allowed for this case: {params['model']}")
    if "tools" in params and not isinstance(params["tools"], list):
        raise BrokerProtocolError("broker tools must be a list")
    if ("max_tokens" not in params
            or isinstance(params["max_tokens"], bool)
            or not isinstance(params["max_tokens"], int)):
        raise BrokerProtocolError("broker max_tokens must be an integer")
    if params["max_tokens"] <= 0:
        raise BrokerProtocolError("broker max_tokens must be greater than zero")
    if params["max_tokens"] > max_tokens_per_call:
        raise BrokerProtocolError(
            f"broker max_tokens exceeds the per-call limit ({max_tokens_per_call})")
    return params


class BrokerModelClient:
    """Container-side model client exposing only messages.create over file IPC."""

    def __init__(
        self,
        ipc_root: str | Path,
        nonce: str,
        *,
        request_timeout: float = 30,
        case_deadline: float | None = None,
        poll_interval: float = 0.02,
        max_calls: int | None = None,
        max_provider_retries: int = 0,
    ):
        self.ipc_root = Path(ipc_root).resolve()
        self.nonce = _validate_token(nonce, "nonce")
        self.request_timeout = float(request_timeout)
        self.case_deadline = case_deadline
        self.poll_interval = float(poll_interval)
        self.max_calls = int(max_calls) if max_calls is not None else 0
        self.max_provider_retries = max(0, int(max_provider_retries))
        self._logical_request_count = 0
        self.messages = self

    def budget_snapshot(self) -> dict:
        """Read the Broker's live, non-secret budget counters."""
        payload = None
        # The nested location is mounted read-only into Docker. Keep the root
        # fallback for older local callers and previously created fixtures.
        for stats_path in (
            self.ipc_root / "stats" / "broker_stats.json",
            self.ipc_root / "broker_stats.json",
        ):
            try:
                payload = json.loads(stats_path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
        if (not isinstance(payload, dict)
                or payload.get("version") != PROTOCOL_VERSION
                or payload.get("nonce") != self.nonce):
            if self.max_calls > 0:
                return {
                    "source": "configured_fallback",
                    "request_count": self._logical_request_count,
                    "call_count": self._logical_request_count,
                    "max_calls": self.max_calls,
                    "max_provider_retries": self.max_provider_retries,
                }
            return {}
        allowed = {
            "request_count", "call_count", "rejected_count", "retry_count",
            "requested_token_count", "max_calls", "max_tokens_per_call",
            "max_total_tokens", "max_provider_retries",
            "actual_input_token_count", "actual_output_token_count",
            "actual_cache_creation_input_token_count",
            "actual_cache_read_input_token_count", "actual_total_token_count",
            "usage_response_count", "usage_missing_response_count",
        }
        snapshot = {key: payload[key] for key in allowed if key in payload}
        snapshot["source"] = "broker_stats"
        return snapshot

    def create(self, *, model: str, messages: list, system=None, tools=None,
               max_tokens: int = 8000, **kwargs):
        if kwargs:
            raise BrokerProtocolError(
                "messages.create received unsupported arguments: "
                + ", ".join(sorted(kwargs)))
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise BrokerProtocolError("messages.create max_tokens must be an integer")
        request_id = uuid.uuid4().hex
        request_path = _protocol_path(
            self.ipc_root, "requests", self.nonce, request_id)
        response_path = _protocol_path(
            self.ipc_root, "responses", self.nonce, request_id)
        params = {
            "model": str(model),
            "messages": _message_value(messages),
            "max_tokens": int(max_tokens),
        }
        if system is not None:
            params["system"] = str(system)
        if tools is not None:
            params["tools"] = _message_value(tools)
        request = {
            "version": PROTOCOL_VERSION,
            "nonce": self.nonce,
            "request_id": request_id,
            "method": "messages.create",
            "params": params,
        }
        # This is a conservative fallback when the live stats mount is briefly
        # unavailable. Broker stats supersede it as soon as they can be read.
        self._logical_request_count += 1
        _atomic_write_json(request_path, request)
        deadline = time.monotonic() + self.request_timeout
        if self.case_deadline is not None:
            deadline = min(deadline, self.case_deadline)
        try:
            while True:
                if response_path.is_file():
                    payload = json.loads(response_path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise BrokerProtocolError("broker response must be an object")
                    if (payload.get("version") != PROTOCOL_VERSION
                            or payload.get("nonce") != self.nonce
                            or payload.get("request_id") != request_id):
                        raise BrokerProtocolError("broker response identity mismatch")
                    if not payload.get("ok"):
                        error = str(payload.get("error") or "model broker failed")
                        if error.startswith("BrokerProtocolError:"):
                            raise BrokerProtocolError(error)
                        raise BrokerRemoteError(
                            str(payload.get("error_kind") or "broker_remote_error"),
                            error,
                            request_id,
                        )
                    return _response_object(payload.get("response"))
                if time.monotonic() >= deadline:
                    raise BrokerIpcTimeout(request_id)
                time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        finally:
            for path in (request_path, response_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


class ModelBroker:
    """Host-side, per-case broker for the single permitted model RPC."""

    def __init__(self, ipc_root: str | Path, nonce: str, model_client, *,
                 allowed_model: str,
                 case_deadline: float | None = None, poll_interval: float = 0.02,
                 max_calls: int = 32,
                 max_tokens_per_call: int = MAX_TOKENS_PER_CALL,
                 max_total_tokens: int | None = None,
                 provider_timeout: float | None = None,
                 max_provider_retries: int = DEFAULT_PROVIDER_RETRIES,
                 provider_retry_delay: float = DEFAULT_PROVIDER_RETRY_DELAY,
                 delivery_grace: float = DEFAULT_IPC_DELIVERY_GRACE):
        self.ipc_root = Path(ipc_root).resolve()
        self.nonce = _validate_token(nonce, "nonce")
        self.model_client = model_client
        self.allowed_model = str(allowed_model)
        if not self.allowed_model:
            raise ValueError("allowed_model must be non-empty")
        self.case_deadline = case_deadline
        self.poll_interval = float(poll_interval)
        self.max_calls = int(max_calls)
        self.max_tokens_per_call = int(max_tokens_per_call)
        if self.max_calls <= 0:
            raise ValueError("max_calls must be greater than zero")
        if self.max_tokens_per_call <= 0:
            raise ValueError("max_tokens_per_call must be greater than zero")
        self.max_total_tokens = int(
            max_total_tokens
            if max_total_tokens is not None
            else self.max_calls * self.max_tokens_per_call)
        if self.max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be greater than zero")
        if provider_timeout is None:
            try:
                provider_timeout = float(os.getenv("MODEL_REQUEST_TIMEOUT", "30"))
            except (TypeError, ValueError):
                provider_timeout = 30.0
        self.provider_timeout = float(provider_timeout)
        self.max_provider_retries = int(max_provider_retries)
        self.provider_retry_delay = float(provider_retry_delay)
        self.delivery_grace = float(delivery_grace)
        if self.provider_timeout <= 0:
            raise ValueError("provider_timeout must be greater than zero")
        if self.max_provider_retries < 0:
            raise ValueError("max_provider_retries cannot be negative")
        if self.provider_retry_delay < 0 or self.delivery_grace < 0:
            raise ValueError("retry delay and delivery grace cannot be negative")
        self.request_count = 0
        self.call_count = 0
        self.rejected_count = 0
        self.requested_token_count = 0
        self.actual_input_token_count = 0
        self.actual_output_token_count = 0
        self.actual_cache_creation_input_token_count = 0
        self.actual_cache_read_input_token_count = 0
        self.actual_total_token_count = 0
        self.usage_response_count = 0
        self.usage_missing_response_count = 0
        self.retry_count = 0
        self.provider_error_count = 0
        self.last_error = ""
        self.last_error_kind = ""
        self.last_provider_error = ""
        self.retry_skipped_reason = ""
        self.last_request_id = ""
        self.last_request_attempts = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handled: set[str] = set()

    def start(self):
        (self.ipc_root / "requests").mkdir(parents=True, exist_ok=True)
        (self.ipc_root / "responses").mkdir(parents=True, exist_ok=True)
        (self.ipc_root / "stats").mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self.serve_forever,
            name=f"codepilot-model-broker-{self.nonce[:8]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def serve_forever(self):
        """Serve synchronously; eval orchestration runs this in a killable process."""
        (self.ipc_root / "requests").mkdir(parents=True, exist_ok=True)
        (self.ipc_root / "responses").mkdir(parents=True, exist_ok=True)
        (self.ipc_root / "stats").mkdir(parents=True, exist_ok=True)
        self._write_stats()
        self._serve()

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0, timeout))
        return self._thread is None or not self._thread.is_alive()

    def _serve(self):
        request_dir = (self.ipc_root / "requests").resolve()
        pattern = f"{self.nonce}-*.json"
        while not self._stop.is_set():
            if self.case_deadline is not None and time.monotonic() >= self.case_deadline:
                return
            for request_path in sorted(request_dir.glob(pattern)):
                if request_path.name in self._handled:
                    continue
                self._handled.add(request_path.name)
                self._handle(request_path)
            self._stop.wait(self.poll_interval)

    def _write_stats(self):
        try:
            _atomic_write_json(self.ipc_root / "stats" / "broker_stats.json", {
                "version": PROTOCOL_VERSION,
                "nonce": self.nonce,
                "allowed_model": self.allowed_model,
                "request_count": self.request_count,
                "call_count": self.call_count,
                "rejected_count": self.rejected_count,
                "requested_token_count": self.requested_token_count,
                "actual_input_token_count": self.actual_input_token_count,
                "actual_output_token_count": self.actual_output_token_count,
                "actual_cache_creation_input_token_count": (
                    self.actual_cache_creation_input_token_count),
                "actual_cache_read_input_token_count": (
                    self.actual_cache_read_input_token_count),
                "actual_total_token_count": self.actual_total_token_count,
                "usage_response_count": self.usage_response_count,
                "usage_missing_response_count": self.usage_missing_response_count,
                "retry_count": self.retry_count,
                "provider_error_count": self.provider_error_count,
                "max_calls": self.max_calls,
                "max_tokens_per_call": self.max_tokens_per_call,
                "max_total_tokens": self.max_total_tokens,
                "provider_timeout": self.provider_timeout,
                "max_provider_retries": self.max_provider_retries,
                "provider_retry_delay": self.provider_retry_delay,
                "delivery_grace": self.delivery_grace,
                "last_error": self.last_error,
                "last_error_kind": self.last_error_kind,
                "last_provider_error": self.last_provider_error,
                "retry_skipped_reason": self.retry_skipped_reason,
                "last_request_id": self.last_request_id,
                "last_request_attempts": self.last_request_attempts,
            })
        except OSError:
            pass

    def _reserve_provider_attempt(self, requested_tokens: int):
        if self.case_deadline is not None and time.monotonic() >= self.case_deadline:
            raise TimeoutError("eval case deadline exceeded before model request")
        if self.call_count >= self.max_calls:
            raise BrokerProtocolError("model broker call limit exceeded")
        if self.requested_token_count + requested_tokens > self.max_total_tokens:
            raise BrokerProtocolError("model broker token budget exceeded")
        self.call_count += 1
        self.requested_token_count += requested_tokens
        self.last_request_attempts += 1
        self._write_stats()

    def _retry_block_reason(self, requested_tokens: int) -> str:
        if self._stop.is_set():
            return "broker_stopping"
        if self.call_count >= self.max_calls:
            return "call_budget"
        if self.requested_token_count + requested_tokens > self.max_total_tokens:
            return "token_budget"
        if self.case_deadline is not None:
            remaining = self.case_deadline - time.monotonic()
            required = (
                self.provider_retry_delay
                + self.provider_timeout
                + self.delivery_grace
            )
            if remaining < required:
                return "case_deadline"
        return ""

    def _call_provider_once(self, params: dict):
        old_timeout = os.environ.get("MODEL_REQUEST_TIMEOUT")
        timeout = self.provider_timeout
        if self.case_deadline is not None:
            timeout = min(
                timeout,
                max(0.1, self.case_deadline - time.monotonic()),
            )
        os.environ["MODEL_REQUEST_TIMEOUT"] = str(timeout)
        try:
            return self.model_client.messages.create(**params)
        finally:
            if old_timeout is None:
                os.environ.pop("MODEL_REQUEST_TIMEOUT", None)
            else:
                os.environ["MODEL_REQUEST_TIMEOUT"] = old_timeout

    def _call_provider_with_retry(self, params: dict):
        retries_used = 0
        requested_tokens = params["max_tokens"]
        while True:
            self._reserve_provider_attempt(requested_tokens)
            try:
                response = self._call_provider_once(params)
                usage = _response_usage(response)
                if usage:
                    self.actual_input_token_count += usage["input_tokens"]
                    self.actual_output_token_count += usage["output_tokens"]
                    self.actual_cache_creation_input_token_count += (
                        usage["cache_creation_input_tokens"])
                    self.actual_cache_read_input_token_count += (
                        usage["cache_read_input_tokens"])
                    self.actual_total_token_count += usage["total_tokens"]
                    self.usage_response_count += 1
                else:
                    self.usage_missing_response_count += 1
                self._write_stats()
                return response
            except Exception as exc:
                error_kind = _provider_error_kind(exc)
                self.provider_error_count += 1
                self.last_provider_error = (
                    f"{error_kind}: {type(exc).__name__}: {exc}")
                if (self.case_deadline is not None
                        and time.monotonic() >= self.case_deadline):
                    raise _ProviderCallError("case_timeout", exc) from exc
                if (not _is_transient_provider_error(error_kind)
                        or retries_used >= self.max_provider_retries):
                    raise _ProviderCallError(error_kind, exc) from exc
                blocked = self._retry_block_reason(requested_tokens)
                if blocked:
                    self.retry_skipped_reason = blocked
                    raise _ProviderCallError(error_kind, exc) from exc
                retries_used += 1
                self.retry_count += 1
                self._write_stats()
                if self._stop.wait(self.provider_retry_delay):
                    self.retry_skipped_reason = "broker_stopping"
                    raise _ProviderCallError(error_kind, exc) from exc

    def _handle(self, request_path: Path):
        request_id = request_path.stem.removeprefix(f"{self.nonce}-")
        try:
            _validate_token(request_id, "request id")
        except BrokerProtocolError as exc:
            self.last_error = str(exc)
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        try:
            expected = _protocol_path(
                self.ipc_root, "requests", self.nonce, request_id)
            if expected != request_path.resolve():
                raise BrokerProtocolError("broker request path mismatch")
            if request_path.is_symlink():
                raise BrokerProtocolError("broker request may not be a symlink")
            if request_path.stat().st_size > MAX_REQUEST_BYTES:
                raise BrokerProtocolError("broker request exceeds the size limit")
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            params = _validate_request(
                payload,
                nonce=self.nonce,
                request_id=request_id,
                allowed_model=self.allowed_model,
                max_tokens_per_call=self.max_tokens_per_call,
            )
            self.request_count += 1
            self.last_request_id = request_id
            self.last_request_attempts = 0
            self.last_error = ""
            self.last_error_kind = ""
            self.retry_skipped_reason = ""
            response = self._call_provider_with_retry(params)
            result = {
                "version": PROTOCOL_VERSION,
                "nonce": self.nonce,
                "request_id": request_id,
                "ok": True,
                "response": _response_payload(response),
            }
        except BaseException as exc:
            self.rejected_count += 1
            if isinstance(exc, _ProviderCallError):
                self.last_error_kind = exc.error_kind
                self.last_error = (
                    f"{type(exc.original).__name__}: {exc.original}")
            elif isinstance(exc, BrokerProtocolError):
                self.last_error_kind = "broker_protocol_error"
                self.last_error = f"{type(exc).__name__}: {exc}"
            elif isinstance(exc, TimeoutError):
                self.last_error_kind = "case_timeout"
                self.last_error = f"{type(exc).__name__}: {exc}"
            else:
                self.last_error_kind = "broker_error"
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._write_stats()
            result = {
                "version": PROTOCOL_VERSION,
                "nonce": self.nonce,
                "request_id": request_id,
                "ok": False,
                "error": self.last_error,
                "error_kind": self.last_error_kind,
            }
        response_path = _protocol_path(
            self.ipc_root, "responses", self.nonce, request_id)
        _atomic_write_json(response_path, result)
        self._write_stats()
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
