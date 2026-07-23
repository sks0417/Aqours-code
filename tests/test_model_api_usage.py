from __future__ import annotations

from codepilot_s20.model_api import _openai_message_to_response


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
