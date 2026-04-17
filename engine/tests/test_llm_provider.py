"""Tests for LLM provider utilities (mocked — no real API calls)."""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from knowler_engine.llm.provider import parse_llm_json, LLMResponse, LLMProvider, LLMError


def test_parse_llm_json_plain_json():
    raw = '{"entities": [], "claims": []}'
    result = parse_llm_json(raw)
    assert result == {"entities": [], "claims": []}


def test_parse_llm_json_strips_markdown_fence():
    raw = '```json\n{"key": "value"}\n```'
    result = parse_llm_json(raw)
    assert result == {"key": "value"}


def test_parse_llm_json_strips_plain_fence():
    raw = '```\n{"key": "value"}\n```'
    result = parse_llm_json(raw)
    assert result == {"key": "value"}


def test_parse_llm_json_with_leading_text():
    raw = 'Here is the JSON:\n```json\n{"result": true}\n```'
    result = parse_llm_json(raw)
    assert result == {"result": True}


def test_parse_llm_json_invalid_raises():
    with pytest.raises((json.JSONDecodeError, ValueError)):
        parse_llm_json("this is not json at all")


def test_parse_llm_json_empty_object():
    result = parse_llm_json("{}")
    assert result == {}


def test_parse_llm_json_nested():
    raw = '{"entities": [{"name": "AI", "type": "concept"}]}'
    result = parse_llm_json(raw)
    assert result["entities"][0]["name"] == "AI"


def test_llm_response_fields():
    resp = LLMResponse(text="hello", input_tokens=10, output_tokens=5)
    assert resp.text == "hello"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5


def test_llm_response_estimated_cost_non_negative():
    resp = LLMResponse(text="hello", input_tokens=100, output_tokens=50)
    assert resp.estimated_cost_usd >= 0


def test_llm_provider_not_configured_raises():
    """Provider with no API key should raise LLMError on complete()."""
    provider = LLMProvider(provider="anthropic", api_key=None)
    # Override resolved key to ensure it's None
    provider._api_key = None

    import asyncio
    with pytest.raises(LLMError, match="No API key"):
        asyncio.get_event_loop().run_until_complete(
            provider.complete("system", "user", tier="fast")
        )


def test_llm_provider_is_configured_false_without_key():
    provider = LLMProvider(provider="anthropic", api_key=None)
    provider._api_key = None
    assert provider.is_configured() is False


def test_llm_provider_is_configured_true_with_key():
    provider = LLMProvider(provider="anthropic", api_key="sk-ant-test")
    assert provider.is_configured() is True


def test_llm_provider_model_for_tier():
    provider = LLMProvider(provider="anthropic", api_key="test")
    fast_model = provider.model_for("fast")
    balanced_model = provider.model_for("balanced")
    best_model = provider.model_for("best")
    assert "haiku" in fast_model or "fast" in fast_model or fast_model != balanced_model
    assert balanced_model != best_model


def test_llm_provider_switches_provider_and_models():
    provider = LLMProvider(provider="anthropic", api_key="anthropic-key")

    provider.set_provider("openai", "openai-key")

    assert provider.provider == "openai"
    assert provider.is_configured() is True
    assert provider.model_for("fast") == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_llm_provider_complete_anthropic_mocked():
    """Verify complete() dispatches to _call_anthropic."""
    provider = LLMProvider(provider="anthropic", api_key="test-key")
    mock_response = LLMResponse(text='{"ok": true}', input_tokens=100, output_tokens=20)

    with patch.object(provider, "_call_anthropic", AsyncMock(return_value=mock_response)):
        result = await provider.complete("system prompt", "user message", tier="fast")

    assert result.text == '{"ok": true}'


@pytest.mark.asyncio
async def test_llm_provider_uses_balanced_model():
    from knowler_engine.llm.provider import _ANTHROPIC_MODELS

    provider = LLMProvider(provider="anthropic", api_key="test-key")
    calls = []

    async def mock_call(system_prompt, user_message, model, max_tokens, response_format):
        calls.append(model)
        return LLMResponse(text="{}", input_tokens=1, output_tokens=1)

    with patch.object(provider, "_call_anthropic", mock_call):
        await provider.complete("sys", "user", tier="balanced")

    assert calls[0] == _ANTHROPIC_MODELS["balanced"]
