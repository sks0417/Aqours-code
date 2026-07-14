from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 8 * 1024 * 1024
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ALLOWED_CREATE_KEYS = {"model", "system", "messages", "tools", "max_tokens"}


class BrokerProtocolError(RuntimeError):
    pass


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


def _response_payload(response) -> dict:
    return {
        "content": _message_value(getattr(response, "content", [])),
        "stop_reason": getattr(response, "stop_reason", None),
    }


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
    return SimpleNamespace(
        content=blocks,
        stop_reason=payload.get("stop_reason") or "end_turn",
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


def _validate_request(payload: dict, *, nonce: str, request_id: str) -> dict:
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
    if "tools" in params and not isinstance(params["tools"], list):
        raise BrokerProtocolError("broker tools must be a list")
    if "max_tokens" in params and not isinstance(params["max_tokens"], int):
        raise BrokerProtocolError("broker max_tokens must be an integer")
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
    ):
        self.ipc_root = Path(ipc_root).resolve()
        self.nonce = _validate_token(nonce, "nonce")
        self.request_timeout = float(request_timeout)
        self.case_deadline = case_deadline
        self.poll_interval = float(poll_interval)
        self.messages = self

    def create(self, *, model: str, messages: list, system=None, tools=None,
               max_tokens: int = 8000, **kwargs):
        if kwargs:
            raise BrokerProtocolError(
                "messages.create received unsupported arguments: "
                + ", ".join(sorted(kwargs)))
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
                        raise RuntimeError(str(payload.get("error") or "model broker failed"))
                    return _response_object(payload.get("response"))
                if time.monotonic() >= deadline:
                    raise TimeoutError("model broker request exceeded its deadline")
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
                 case_deadline: float | None = None, poll_interval: float = 0.02,
                 max_calls: int = 256):
        self.ipc_root = Path(ipc_root).resolve()
        self.nonce = _validate_token(nonce, "nonce")
        self.model_client = model_client
        self.case_deadline = case_deadline
        self.poll_interval = float(poll_interval)
        self.max_calls = max(1, int(max_calls))
        self.call_count = 0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handled: set[str] = set()

    def start(self):
        (self.ipc_root / "requests").mkdir(parents=True, exist_ok=True)
        (self.ipc_root / "responses").mkdir(parents=True, exist_ok=True)
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
            _atomic_write_json(self.ipc_root / "broker_stats.json", {
                "version": PROTOCOL_VERSION,
                "nonce": self.nonce,
                "call_count": self.call_count,
                "last_error": self.last_error,
            })
        except OSError:
            pass

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
                payload, nonce=self.nonce, request_id=request_id)
            if self.case_deadline is not None and time.monotonic() >= self.case_deadline:
                raise TimeoutError("eval case deadline exceeded before model request")
            if self.call_count >= self.max_calls:
                raise BrokerProtocolError("model broker call limit exceeded")
            self.call_count += 1
            self._write_stats()
            old_timeout = os.environ.get("MODEL_REQUEST_TIMEOUT")
            if self.case_deadline is not None:
                remaining = max(0.1, self.case_deadline - time.monotonic())
                try:
                    configured = float(old_timeout or "30")
                except (TypeError, ValueError):
                    configured = 30.0
                os.environ["MODEL_REQUEST_TIMEOUT"] = str(min(configured, remaining))
            try:
                response = self.model_client.messages.create(**params)
            finally:
                if self.case_deadline is not None:
                    if old_timeout is None:
                        os.environ.pop("MODEL_REQUEST_TIMEOUT", None)
                    else:
                        os.environ["MODEL_REQUEST_TIMEOUT"] = old_timeout
            result = {
                "version": PROTOCOL_VERSION,
                "nonce": self.nonce,
                "request_id": request_id,
                "ok": True,
                "response": _response_payload(response),
            }
        except BaseException as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._write_stats()
            result = {
                "version": PROTOCOL_VERSION,
                "nonce": self.nonce,
                "request_id": request_id,
                "ok": False,
                "error": self.last_error,
            }
        response_path = _protocol_path(
            self.ipc_root, "responses", self.nonce, request_id)
        _atomic_write_json(response_path, result)
        self._write_stats()
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
