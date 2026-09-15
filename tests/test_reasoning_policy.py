from __future__ import annotations

import pytest

from voice_terminal.providers.deepseek import DeepSeekClient, DeepSeekError
from voice_terminal.shadow_agent.provider import DeepSeekResponsesShadowProvider


def test_chat_and_responses_payloads_use_the_product_reasoning_vocabulary() -> None:
    client = DeepSeekClient(
        base_url="https://example.test",
        model="voice-model",
        api_key="in-memory-only",
        reasoning_effort="medium",
        allowed_reasoning=("off", "medium"),
        thinking="enabled",
    )

    disabled = client._request_payload([], reasoning_effort="off")
    assert disabled["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in disabled

    medium = client._request_payload([], reasoning_effort="medium")
    assert medium["thinking"] == {"type": "enabled"}
    assert medium["reasoning_effort"] == "medium"

    responses_off = client._responses_payload(
        [], model=client.model, schema={}, schema_name="test", reasoning_effort="off", max_output_tokens=32,
    )
    responses_medium = client._responses_payload(
        [], model=client.model, schema={}, schema_name="test", reasoning_effort="medium", max_output_tokens=32,
    )
    assert responses_off["reasoning"] == {"effort": "none"}
    assert responses_medium["reasoning"] == {"effort": "medium"}

    with pytest.raises(DeepSeekError, match="不支持思考等级 low"):
        client._request_payload([], reasoning_effort="low")


def test_shadow_responses_payload_maps_off_to_provider_none() -> None:
    provider = DeepSeekResponsesShadowProvider(
        base_url="https://example.test", model="shadow-model", api_key="in-memory-only",
    )
    payload = provider._payload(
        instructions="safe",
        input_items=[],
        tools=[],
        reasoning_effort="off",
        text_schema=None,
        max_output_tokens=32,
    )
    assert payload["reasoning"] == {"effort": "none"}
