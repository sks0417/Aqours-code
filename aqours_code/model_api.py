from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace


DEEPSEEK_V4_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}
DEEPSEEK_REASONING_EFFORT = "high"
DEEPSEEK_THINKING_ESCALATED_MAX_TOKENS = 128_000


def uses_deepseek_thinking(provider_name: str, model: str) -> bool:
    return (
        str(provider_name).casefold() == "deepseek"
        and str(model).casefold() in DEEPSEEK_V4_MODELS
    )


def effective_initial_max_tokens(
    provider_name: str,
    model: str,
    *,
    configured_default_max_tokens: int,
) -> int:
    if uses_deepseek_thinking(provider_name, model):
        return max(
            int(configured_default_max_tokens),
            DEEPSEEK_THINKING_ESCALATED_MAX_TOKENS,
        )
    return int(configured_default_max_tokens)


def effective_escalated_max_tokens(
    provider_name: str,
    model: str,
    *,
    current_max_tokens: int,
    configured_escalated_max_tokens: int,
) -> int:
    provider_escalation = (
        DEEPSEEK_THINKING_ESCALATED_MAX_TOKENS
        if uses_deepseek_thinking(provider_name, model)
        else configured_escalated_max_tokens
    )
    return max(
        int(current_max_tokens),
        int(configured_escalated_max_tokens),
        int(provider_escalation),
    )


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value or None


def _validate_api_key(api_key: str | None, provider: str) -> str:
    if not api_key:
        raise RuntimeError(f"Missing API key for {provider}")
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise RuntimeError(f"{provider} API key contains non-ASCII characters. Replace the placeholder text in .env with the real API key, for example sk-...") from exc
    if any(token in api_key for token in ["??", "your_", "YOUR_", "<", ">"]):
        raise RuntimeError(f"{provider} API key still looks like a placeholder. Set it to the real key value from the provider console.")
    return api_key


def _request_timeout() -> float:
    try:
        return max(1.0, float(os.getenv("AQOURS_CODE_REQUEST_TIMEOUT", "30")))
    except (TypeError, ValueError):
        return 30.0


def _block_to_dict(block):
    if isinstance(block, dict):
        return block
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if kind == "tool_use":
        return {"type": "tool_use", "id": getattr(block, "id", ""), "name": getattr(block, "name", ""), "input": getattr(block, "input", {})}
    return {"type": kind or "text", "text": str(block)}


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        data = _block_to_dict(block)
        if data.get("type") == "text":
            parts.append(str(data.get("text", "")))
    return "\n".join(part for part in parts if part)


def _anthropic_content_to_openai(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    return _extract_text(content) or ""


def _messages_to_openai(messages: list[dict]) -> list[dict]:
    converted: list[dict] = []
    pending_tool_calls = {}
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            tool_calls = []
            text_parts = []
            if isinstance(content, list):
                for block in content:
                    data = _block_to_dict(block)
                    if data.get("type") == "text":
                        text_parts.append(str(data.get("text", "")))
                    elif data.get("type") == "tool_use":
                        call_id = data.get("id")
                        pending_tool_calls[call_id] = data.get("name", "")
                        tool_calls.append({"id": call_id, "type": "function", "function": {"name": data.get("name", ""), "arguments": json.dumps(data.get("input", {}), ensure_ascii=False)}})
            else:
                text_parts.append(str(content or ""))
            msg = {"role": "assistant", "content": "\n".join(part for part in text_parts if part) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            reasoning_content = message.get("reasoning_content")
            if reasoning_content is not None:
                msg["reasoning_content"] = str(reasoning_content)
            # OpenAI-compatible providers reject assistant messages that have
            # neither visible content nor tool calls. This can happen when a
            # thinking model exhausts max_tokens before producing an answer.
            # Incomplete reasoning alone is not a replayable assistant turn.
            if msg["content"] is None and not tool_calls:
                continue
            converted.append(msg)
            continue
        if role == "user" and isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    converted.append({"role": "tool", "tool_call_id": tool_use_id, "name": pending_tool_calls.get(tool_use_id, "tool"), "content": str(block.get("content", ""))})
                elif isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                else:
                    text_parts.append(str(block))
            if text_parts:
                converted.append({"role": "user", "content": "\n".join(text_parts)})
            continue
        converted.append({"role": role, "content": _anthropic_content_to_openai(content)})
    return converted


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool.get("input_schema") or tool.get("parameters") or {}}} for tool in tools or []]


def _openai_message_to_response(
    message: dict,
    finish_reason: str | None,
    usage: dict | None = None,
):
    content = []
    text = message.get("content")
    if text:
        content.append(SimpleNamespace(type="text", text=text))
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
        content.append(SimpleNamespace(type="tool_use", id=call.get("id", ""), name=function.get("name", ""), input=args))
    stop_reason = "tool_use" if message.get("tool_calls") else "end_turn"
    if finish_reason == "length":
        stop_reason = "max_tokens"
    usage_object = SimpleNamespace(**usage) if isinstance(usage, dict) else None
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=usage_object,
        reasoning_content=message.get("reasoning_content"),
    )


def assistant_message_from_response(response) -> dict:
    """Preserve provider state that must be replayed on the next tool turn."""
    message = {"role": "assistant", "content": response.content}
    reasoning_content = getattr(response, "reasoning_content", None)
    if reasoning_content is not None:
        message["reasoning_content"] = str(reasoning_content)
    return message


class OpenAICompatibleMessages:
    def __init__(self, api_key: str | None, base_url: str, extra_headers: dict | None = None, provider_name: str = "OpenAI-compatible provider"):
        self.api_key = _clean_env(api_key)
        self.base_url = base_url.rstrip("/")
        self.extra_headers = extra_headers or {}
        self.provider_name = provider_name

    def create(self, *, model: str, system: str | None = None,
               messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int = 8000, thinking: dict | None = None,
               **kwargs):
        api_key = _validate_api_key(self.api_key, self.provider_name)
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(_messages_to_openai(messages))
        payload = {"model": model, "messages": payload_messages, "max_tokens": max_tokens}
        deepseek_thinking = uses_deepseek_thinking(
            self.provider_name, model,
        )
        if deepseek_thinking:
            thinking_config = (
                thinking if thinking is not None else {"type": "enabled"}
            )
            if thinking_config not in (
                {"type": "enabled"},
                {"type": "disabled"},
            ):
                raise ValueError("invalid DeepSeek thinking configuration")
            payload["thinking"] = thinking_config
            if thinking_config["type"] == "enabled":
                payload["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
        if tools:
            payload["tools"] = _tools_to_openai(tools)
            if not deepseek_thinking:
                payload["tool_choice"] = "auto"
        payload.update(kwargs)
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", **self.extra_headers}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=_request_timeout()) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Model request failed: HTTP {exc.code}: {detail[:2000]}") from exc
        choice = data["choices"][0]
        return _openai_message_to_response(
            choice.get("message", {}),
            choice.get("finish_reason"),
            data.get("usage"),
        )


class OpenAICompatibleClient:
    def __init__(self, api_key: str | None, base_url: str, extra_headers: dict | None = None, provider_name: str = "OpenAI-compatible provider"):
        self.messages = OpenAICompatibleMessages(api_key, base_url, extra_headers, provider_name)


class AnthropicClient:
    def __init__(self, api_key: str | None, base_url: str | None = None):
        try:
            from anthropic import Anthropic
        except ImportError:
            self.messages = self
            return
        self._client = Anthropic(
            api_key=_validate_api_key(_clean_env(api_key), "Anthropic"),
            base_url=base_url or None,
        )
        self.messages = self._client.messages

    def create(self, *args, **kwargs):
        raise RuntimeError("anthropic is not installed. Run `pip install -e .` first.")


def sanitize_base_url(base_url: str | None) -> str:
    """Remove credentials and auth-like query values before metadata/logging."""
    cleaned = _clean_env(base_url) or ""
    if not cleaned:
        return ""
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        hostname = parsed.hostname or ""
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        safe_query = urllib.parse.urlencode([
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
            if not any(
                marker in key.casefold()
                for marker in ("key", "token", "secret", "password", "auth")
            )
        ])
        return urllib.parse.urlunsplit(
            (parsed.scheme, hostname, parsed.path, safe_query, "")
        )
    except (TypeError, ValueError):
        return "[invalid base URL]"


def build_model_client(
    provider: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
):
    api_key = _clean_env(api_key) or _clean_env(os.getenv("AQOURS_CODE_API_KEY"))
    base_url = _clean_env(base_url) or _clean_env(os.getenv("AQOURS_CODE_BASE_URL"))
    if provider == "anthropic":
        return AnthropicClient(api_key=api_key, base_url=base_url)
    if provider == "deepseek":
        return OpenAICompatibleClient(api_key=api_key, base_url=base_url or "", provider_name="DeepSeek")
    if provider == "openai":
        return OpenAICompatibleClient(api_key=api_key, base_url=base_url or "", provider_name="OpenAI")
    if provider == "openai_compatible":
        return OpenAICompatibleClient(api_key=api_key, base_url=base_url or "", provider_name="OpenAI-compatible provider")
    raise ValueError(f"Unknown AQOURS_CODE_PROVIDER: {provider}")


def provider_from_env() -> str:
    return os.getenv("AQOURS_CODE_PROVIDER", "openai_compatible").strip().lower()
