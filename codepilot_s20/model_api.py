from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace


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
        return max(1.0, float(os.getenv("MODEL_REQUEST_TIMEOUT", "30")))
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


def _openai_message_to_response(message: dict, finish_reason: str | None):
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
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class OpenAICompatibleMessages:
    def __init__(self, api_key: str | None, base_url: str, extra_headers: dict | None = None, provider_name: str = "OpenAI-compatible provider"):
        self.api_key = _clean_env(api_key)
        self.base_url = base_url.rstrip("/")
        self.extra_headers = extra_headers or {}
        self.provider_name = provider_name

    def create(self, *, model: str, system: str | None = None, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 8000, **kwargs):
        api_key = _validate_api_key(self.api_key, self.provider_name)
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(_messages_to_openai(messages))
        payload = {"model": model, "messages": payload_messages, "max_tokens": max_tokens}
        if tools:
            payload["tools"] = _tools_to_openai(tools)
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
        return _openai_message_to_response(choice.get("message", {}), choice.get("finish_reason"))


class OpenAICompatibleClient:
    def __init__(self, api_key: str | None, base_url: str, extra_headers: dict | None = None, provider_name: str = "OpenAI-compatible provider"):
        self.messages = OpenAICompatibleMessages(api_key, base_url, extra_headers, provider_name)


class AnthropicClient:
    def __init__(self, base_url: str | None = None):
        try:
            from anthropic import Anthropic
        except ImportError:
            self.messages = self
            return
        self._client = Anthropic(base_url=base_url)
        self.messages = self._client.messages

    def create(self, *args, **kwargs):
        raise RuntimeError("anthropic is not installed. Run `pip install -e .` first.")


def default_model_for_provider(provider: str) -> str:
    if provider == "deepseek":
        return "deepseek-chat"
    if provider in {"openai", "openai_compatible"}:
        return "gpt-4o-mini"
    return "claude-3-5-sonnet-latest"


def build_model_client(provider: str):
    if provider == "anthropic":
        return AnthropicClient(base_url=os.getenv("ANTHROPIC_BASE_URL"))
    if provider == "deepseek":
        return OpenAICompatibleClient(api_key=_clean_env(os.getenv("DEEPSEEK_API_KEY")) or _clean_env(os.getenv("MODEL_API_KEY")), base_url=_clean_env(os.getenv("DEEPSEEK_BASE_URL")) or _clean_env(os.getenv("MODEL_BASE_URL")) or "https://api.deepseek.com", provider_name="DeepSeek")
    if provider == "openai":
        return OpenAICompatibleClient(api_key=_clean_env(os.getenv("OPENAI_API_KEY")) or _clean_env(os.getenv("MODEL_API_KEY")), base_url=_clean_env(os.getenv("OPENAI_BASE_URL")) or _clean_env(os.getenv("MODEL_BASE_URL")) or "https://api.openai.com/v1", provider_name="OpenAI")
    if provider == "openai_compatible":
        return OpenAICompatibleClient(api_key=_clean_env(os.getenv("MODEL_API_KEY")) or _clean_env(os.getenv("OPENAI_API_KEY")), base_url=_clean_env(os.getenv("MODEL_BASE_URL")) or _clean_env(os.getenv("OPENAI_BASE_URL")) or "https://api.openai.com/v1", provider_name="OpenAI-compatible provider")
    raise ValueError(f"Unknown MODEL_PROVIDER: {provider}")


def provider_from_env() -> str:
    return os.getenv("MODEL_PROVIDER", "anthropic").strip().lower()
