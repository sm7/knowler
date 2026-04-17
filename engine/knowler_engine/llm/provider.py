"""
LLM provider adapter.

Provides a thin abstraction over LLM API calls so the rest of the engine
doesn't depend on a specific SDK.

LLM is only used where the architecture doc says:
  - source normalization and enrichment
  - concept and claim extraction
  - knowledge compilation into wiki pages
  - query planning
  - final answer/artifact generation
  - semantic maintenance suggestions

NOT used for:
  - deterministic indexing, file movement, storage, locks, deduplication
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

ModelTier = Literal["fast", "balanced", "best"]

# Default models per tier (Anthropic Claude)
_ANTHROPIC_MODELS: dict[ModelTier, str] = {
    "fast": "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
    "best": "claude-opus-4-6",
}

_OPENAI_MODELS: dict[ModelTier, str] = {
    "fast": "gpt-4o-mini",
    "balanced": "gpt-4o",
    "best": "gpt-4o",
}


def _models_for_provider(provider: str) -> dict[ModelTier, str]:
    return _OPENAI_MODELS if provider == "openai" else _ANTHROPIC_MODELS


class LLMError(Exception):
    """Raised when an LLM call fails."""


class LLMResponse:
    def __init__(self, text: str, input_tokens: int, output_tokens: int) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        # Very rough ballpark — not accounting for model differences
        return (self.input_tokens * 0.000003) + (self.output_tokens * 0.000015)


class LLMProvider:
    """
    Async LLM caller. Supports Anthropic and OpenAI.

    API key is resolved in this priority:
    1. Passed explicitly via api_key parameter
    2. Environment variable (KNOWLER_LLM_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY)
    3. macOS Keychain via `keyring` library (service='Knowler', username=provider)
    """

    def __init__(
        self,
        provider: str = "anthropic",
        api_key: str | None = None,
        models: dict[ModelTier, str] | None = None,
    ) -> None:
        self._provider = provider
        self._api_key = api_key or self._resolve_key(provider)
        self._models = models or _models_for_provider(provider)

    def _resolve_key(self, provider: str) -> str | None:
        """Try env vars then Keychain."""
        # Dev override
        if key := os.environ.get("KNOWLER_LLM_API_KEY"):
            return key
        if provider == "anthropic":
            if key := os.environ.get("ANTHROPIC_API_KEY"):
                return key
        elif provider == "openai":
            if key := os.environ.get("OPENAI_API_KEY"):
                return key

        # macOS Keychain
        try:
            import keyring
            key = keyring.get_password("Knowler", provider)
            return key
        except Exception:
            pass

        return None

    def set_key(self, api_key: str) -> None:
        """Update the API key at runtime (called after user saves key via Settings)."""
        self._api_key = api_key

    def set_provider(self, provider: str, api_key: str | None = None) -> None:
        """Switch providers and refresh the active model map."""
        self._provider = provider
        self._models = _models_for_provider(provider)
        self._api_key = api_key if api_key is not None else self._resolve_key(provider)

    @property
    def provider(self) -> str:
        return self._provider

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def model_for(self, tier: ModelTier) -> str:
        return self._models[tier]

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        tier: ModelTier = "balanced",
        max_tokens: int = 2000,
        response_format: Literal["text", "json"] = "json",
    ) -> LLMResponse:
        """
        Call the configured LLM and return a LLMResponse.

        Raises LLMError on provider failures.
        """
        if not self._api_key:
            raise LLMError(
                f"No API key configured for provider '{self._provider}'. "
                "Set it in Settings or via the KNOWLER_LLM_API_KEY environment variable."
            )

        model = self.model_for(tier)

        if self._provider == "anthropic":
            return await self._call_anthropic(
                system_prompt, user_message, model, max_tokens, response_format
            )
        elif self._provider == "openai":
            return await self._call_openai(
                system_prompt, user_message, model, max_tokens, response_format
            )
        else:
            raise LLMError(f"Unknown provider: {self._provider}")

    async def _call_anthropic(
        self,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        response_format: str,
    ) -> LLMResponse:
        try:
            import anthropic
        except ImportError:
            raise LLMError("anthropic package not installed")

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        try:
            msg = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            text = msg.content[0].text if msg.content else ""
            return LLMResponse(
                text=text,
                input_tokens=msg.usage.input_tokens,
                output_tokens=msg.usage.output_tokens,
            )
        except anthropic.APIError as exc:
            log.error("anthropic_api_error", error=str(exc))
            raise LLMError(f"Anthropic API error: {exc}") from exc

    async def _call_openai(
        self,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        response_format: str,
    ) -> LLMResponse:
        try:
            import openai
        except ImportError:
            raise LLMError("openai package not installed")

        client = openai.AsyncOpenAI(api_key=self._api_key)
        kwargs: dict[str, Any] = {}
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                **kwargs,
            )
            text = resp.choices[0].message.content or ""
            usage = resp.usage
            return LLMResponse(
                text=text,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
            )
        except openai.APIError as exc:
            raise LLMError(f"OpenAI API error: {exc}") from exc


def parse_llm_json(text: str) -> dict[str, Any]:
    """
    Extract and parse the first JSON object from LLM output.
    Handles common LLM quirks like markdown code fences.
    """
    import re
    # Strip markdown code fences
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    # Find first { ... } block
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    return json.loads(text)
