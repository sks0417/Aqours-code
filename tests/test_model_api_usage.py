from __future__ import annotations

import json

from aqours_code.model_api import (
    OpenAICompatibleMessages,
    _messages_to_openai,
    _openai_message_to_response,
    assistant_message_from_response,
    sanitize_base_url,
)


def test_openai_compatible_response_preserves_provider_usage():
    response = _openai_message_to_response(
        {"content": "done"},
        "stop",
        {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
        },
    )

    assert response.usage.prompt_tokens == 120
    assert response.usage.completion_tokens == 30
    assert response.usage.total_tokens == 150


def test_base_url_metadata_removes_credentials_and_auth_query_values():
    assert sanitize_base_url(
        "https://user:password@example.invalid/v1?api-version=1&api_key=secret"
    ) == "https://example.invalid/v1?api-version=1"


def test_deepseek_v4_flash_uses_max_thinking_and_replays_reasoning(
    monkeypatch,
):
    captured = {}

    class FakeHttpResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {
                        "content": None,
                        "reasoning_content": "checked the tool arguments",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"README.md"}',
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeHttpResponse()

    monkeypatch.setattr(
        "aqours_code.model_api.urllib.request.urlopen",
        fake_urlopen,
    )
    messages = OpenAICompatibleMessages(
        "sk-test",
        "https://api.deepseek.com",
        provider_name="DeepSeek",
    )

    response = messages.create(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "inspect the repository"}],
        tools=[{
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object"},
        }],
    )

    payload = captured["payload"]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    assert "tool_choice" not in payload
    assert response.reasoning_content == "checked the tool arguments"

    replayed = _messages_to_openai([
        assistant_message_from_response(response),
    ])
    assert replayed[0]["reasoning_content"] == "checked the tool arguments"
    assert replayed[0]["tool_calls"][0]["function"]["name"] == "read_file"


def test_openai_conversion_drops_empty_assistant_reasoning_turn():
    replayed = _messages_to_openai([
        {"role": "user", "content": "inspect the repository"},
        {
            "role": "assistant",
            "content": [],
            "reasoning_content": "unfinished hidden reasoning",
        },
        {"role": "user", "content": "continue"},
    ])

    assert replayed == [
        {"role": "user", "content": "inspect the repository"},
        {"role": "user", "content": "continue"},
    ]
    assert all(
        message.get("content") or message.get("tool_calls")
        for message in replayed
        if message["role"] == "assistant"
    )


def test_non_deepseek_provider_retains_existing_tool_choice(monkeypatch):
    captured = {}

    class FakeHttpResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "done"},
                    "finish_reason": "stop",
                }],
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHttpResponse()

    monkeypatch.setattr(
        "aqours_code.model_api.urllib.request.urlopen",
        fake_urlopen,
    )
    messages = OpenAICompatibleMessages(
        "sk-test",
        "https://example.invalid/v1",
        provider_name="OpenAI",
    )

    messages.create(
        model="gpt-test",
        messages=[],
        tools=[{"name": "read_file", "input_schema": {}}],
    )

    payload = captured["payload"]
    assert payload["tool_choice"] == "auto"
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload
