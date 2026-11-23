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
# DeepSeek models that get the 128K output limit. Only the output limit:
# thinking, reasoning_effort, and tool_choice still follow DEEPSEEK_V4_MODELS.
DEEPSEEK_128K_OUTPUT_MODELS = DEEPSEEK_V4_MODELS | {"deepseek-flash"}


def uses_deepseek_thinking(provider_name: str, model: str) -> bool:
    return (
        str(provider_name).casefold() == "deepseek"
        and str(model).casefold() in DEEPSEEK_V4_MODELS
    )


def uses_deepseek_128k_output(provider_name: str, model: str) -> bool:
    return (
        str(provider_name).casefold() == "deepseek"
        and str(model).casefold() in DEEPSEEK_128K_OUTPUT_MODELS
    )


# AQOURS_CODE_REASONING_EFFORT (config.parse_reasoning_effort)
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def is_deepseek(provider_name: str, model: str) -> bool:
    """The DeepSeek provider, or a model of the DeepSeek rules above.

    Such requests keep their own thinking setting (uses_deepseek_thinking);
    AQOURS_CODE_REASONING_EFFORT does not change them.
    """
    return (str(provider_name).casefold() == "deepseek"
            or str(model).casefold() in DEEPSEEK_128K_OUTPUT_MODELS)


def is_openrouter(base_url: str | None) -> bool:
    """Whether ``base_url`` is OpenRouter's (openrouter.ai or a subdomain)."""
    try:
        host = (urllib.parse.urlsplit(_clean_env(base_url) or "").hostname or "").casefold()
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def reasoning_effort_fields(provider_name: str, model: str, base_url: str | None,
                            reasoning_effort: str | None) -> dict:
    """The fields an OpenAI-compatible request adds for ``reasoning_effort``.

    None, or a DeepSeek request (is_deepseek), adds nothing. OpenRouter takes
    its unified ``"reasoning": {"effort": ...}`` (the only one it passes on to
    Anthropic models); any other provider takes ``"reasoning_effort"``.
    """
    if not reasoning_effort or is_deepseek(provider_name, model):
        return {}
    if is_openrouter(base_url):
        return {"reasoning": {"effort": reasoning_effort}}
    return {"reasoning_effort": reasoning_effort}


def effective_reasoning_effort(provider_name: str, model: str, base_url: str | None,
                               reasoning_effort: str | None) -> str | None:
    """The thinking level a request to ``model`` asks for, or None if it asks none.

    DeepSeek's thinking models ask DEEPSEEK_REASONING_EFFORT and its other
    models nothing, whatever ``reasoning_effort`` is; the anthropic provider
    (the Anthropic SDK) asks nothing; every other provider asks
    ``reasoning_effort``.
    """
    if uses_deepseek_thinking(provider_name, model):
        return DEEPSEEK_REASONING_EFFORT
    if str(provider_name).casefold() == "anthropic":
        return None
    return reasoning_effort if reasoning_effort_fields(
        provider_name, model, base_url, reasoning_effort) else None


def openrouter_provider_fields(provider_name: str, model: str, base_url: str | None,
                               providers) -> dict:
    """The field an OpenAI-compatible request adds for ``providers``.

    ``providers`` are OpenRouter provider slugs (AQOURS_CODE_OPENROUTER_PROVIDER,
    config.parse_openrouter_provider). Only a request to OpenRouter
    (is_openrouter) that is not DeepSeek's (is_deepseek) gets
    ``"provider": {"order": [...], "allow_fallbacks": false}``: OpenRouter
    then sends it to those providers only, in that order, and fails rather
    than pick another. No providers, or any other request, adds nothing.
    """
    if not providers or not is_openrouter(base_url) or is_deepseek(provider_name, model):
        return {}
    return {"provider": {"order": list(providers), "allow_fallbacks": False}}


def effective_openrouter_provider(provider_name: str, model: str, base_url: str | None,
                                  providers) -> list[str] | None:
    """The OpenRouter providers a request to ``model`` is pinned to, or None if it is not.

    The anthropic provider (the Anthropic SDK) is never pinned; every other
    provider is pinned when openrouter_provider_fields adds the field.
    """
    if str(provider_name).casefold() == "anthropic":
        return None
    fields = openrouter_provider_fields(provider_name, model, base_url, providers)
    return list(fields["provider"]["order"]) if fields else None


def served_provider(value) -> str | None:
    """The provider OpenRouter says served a reply (the response's top-level
    ``provider``, e.g. ``"Z.AI"``), or None when it names none."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _details_value(details, name: str):
    return details.get(name) if isinstance(details, dict) else getattr(details, name, None)


def reasoning_tokens(usage) -> int | None:
    """The reasoning tokens a provider reports in ``usage``, or None if it reports none.

    From ``completion_tokens_details.reasoning_tokens`` (OpenAI-compatible
    chat completions: OpenRouter, DeepSeek, OpenAI),
    ``output_tokens_details.reasoning_tokens``, or ``reasoning_tokens`` itself
    (a usage already read, as the model broker passes it on).
    """
    if usage is None:
        return None
    candidates = [_details_value(usage, "reasoning_tokens")]
    for details_name in ("completion_tokens_details", "output_tokens_details"):
        details = _details_value(usage, details_name)
        if details is not None:
            candidates.append(_details_value(details, "reasoning_tokens"))
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def effective_initial_max_tokens(
    provider_name: str,
    model: str,
    *,
    configured_default_max_tokens: int,
) -> int:
    """The output limit a model call starts with.

    DeepSeek models with a 128K output limit always start at 128K, whatever
    is configured (AQOURS_CODE_MAX_TOKENS); every other model uses
    ``configured_default_max_tokens``.
    """
    if uses_deepseek_128k_output(provider_name, model):
        return DEEPSEEK_THINKING_ESCALATED_MAX_TOKENS
    return int(configured_default_max_tokens)


def effective_escalated_max_tokens(
    provider_name: str,
    model: str,
    *,
    current_max_tokens: int,
    configured_escalated_max_tokens: int,
) -> int:
    """The output limit of the retry after a reply cut at ``current_max_tokens``.

    Never below the current limit. DeepSeek models with a 128K output limit
    use 128K, whatever is configured (AQOURS_CODE_ESCALATED_MAX_TOKENS);
    every other model uses ``configured_escalated_max_tokens``.
    """
    if uses_deepseek_128k_output(provider_name, model):
        return max(int(current_max_tokens), DEEPSEEK_THINKING_ESCALATED_MAX_TOKENS)
    return max(int(current_max_tokens), int(configured_escalated_max_tokens))


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
    provider: str | None = None,
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
        # who served it, when the response says (OpenRouter); None otherwise
        provider=served_provider(provider),
    )


def assistant_message_from_response(response) -> dict:
    """Preserve provider state that must be replayed on the next tool turn."""
    message = {"role": "assistant", "content": response.content}
    reasoning_content = getattr(response, "reasoning_content", None)
    if reasoning_content is not None:
        message["reasoning_content"] = str(reasoning_content)
    return message


class OpenAICompatibleMessages:
    def __init__(self, api_key: str | None, base_url: str, extra_headers: dict | None = None,
                 provider_name: str = "OpenAI-compatible provider",
                 reasoning_effort: str | None = None,
                 openrouter_provider=None):
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"invalid reasoning effort {reasoning_effort!r}")
        if isinstance(openrouter_provider, str):
            raise TypeError("openrouter_provider is a sequence of provider slugs")
        self.api_key = _clean_env(api_key)
        self.base_url = base_url.rstrip("/")
        self.extra_headers = extra_headers or {}
        self.provider_name = provider_name
        # AQOURS_CODE_REASONING_EFFORT; None sends no thinking setting
        self.reasoning_effort = reasoning_effort
        # AQOURS_CODE_OPENROUTER_PROVIDER; None lets OpenRouter pick
        self.openrouter_provider = tuple(openrouter_provider) if openrouter_provider else None

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
        if thinking != {"type": "disabled"}:
            # a call that turns thinking off is left as it is
            payload.update(reasoning_effort_fields(
                self.provider_name, model, self.base_url, self.reasoning_effort))
        # every OpenRouter request, the compaction summary too
        payload.update(openrouter_provider_fields(
            self.provider_name, model, self.base_url, self.openrouter_provider))
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
            data.get("provider"),
        )


class OpenAICompatibleClient:
    def __init__(self, api_key: str | None, base_url: str, extra_headers: dict | None = None,
                 provider_name: str = "OpenAI-compatible provider",
                 reasoning_effort: str | None = None,
                 openrouter_provider=None):
        self.messages = OpenAICompatibleMessages(api_key, base_url, extra_headers, provider_name,
                                                 reasoning_effort, openrouter_provider)


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
    reasoning_effort: str | None = None,
    openrouter_provider=None,
):
    """The client of ``provider``.

    ``reasoning_effort`` (AQOURS_CODE_REASONING_EFFORT) is the thinking level
    the OpenAI-compatible providers ask for (reasoning_effort_fields), and
    ``openrouter_provider`` (AQOURS_CODE_OPENROUTER_PROVIDER) the OpenRouter
    providers their OpenRouter requests are pinned to
    (openrouter_provider_fields); the anthropic provider ignores both.
    """
    api_key = _clean_env(api_key) or _clean_env(os.getenv("AQOURS_CODE_API_KEY"))
    base_url = _clean_env(base_url) or _clean_env(os.getenv("AQOURS_CODE_BASE_URL"))
    settings = {"reasoning_effort": reasoning_effort, "openrouter_provider": openrouter_provider}
    if provider == "anthropic":
        return AnthropicClient(api_key=api_key, base_url=base_url)
    if provider == "deepseek":
        return OpenAICompatibleClient(api_key=api_key, base_url=base_url or "", provider_name="DeepSeek",
                                      **settings)
    if provider == "openai":
        return OpenAICompatibleClient(api_key=api_key, base_url=base_url or "", provider_name="OpenAI",
                                      **settings)
    if provider == "openai_compatible":
        return OpenAICompatibleClient(api_key=api_key, base_url=base_url or "",
                                      provider_name="OpenAI-compatible provider",
                                      **settings)
    raise ValueError(f"Unknown AQOURS_CODE_PROVIDER: {provider}")


def provider_from_env() -> str:
    return os.getenv("AQOURS_CODE_PROVIDER", "openai_compatible").strip().lower()
