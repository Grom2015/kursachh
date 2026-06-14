"""Thin wrapper around the Anthropic SDK.

Handles API key resolution, retries, and a graceful no-op mode when
``ANTHROPIC_API_KEY`` is not set (so the rest of the pipeline keeps working
in fixture/demo mode without an LLM).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import so the module can be imported even when the SDK is absent.
# ---------------------------------------------------------------------------

_anthropic = None


def _ensure_sdk():  # noqa: ANN202
    global _anthropic
    if _anthropic is None:
        try:
            import anthropic

            _anthropic = anthropic
        except ImportError as exc:
            raise RuntimeError(
                "The `anthropic` package is required for LLM features. "
                "Install it with: pip install anthropic"
            ) from exc
    return _anthropic


# ---------------------------------------------------------------------------
# Response wrapper
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    """Normalised LLM response."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    raw: dict[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def extract_json(self) -> dict[str, Any] | None:
        """Try to parse the text body as JSON (with optional ```json fences)."""
        clean = self.text.strip()
        if clean.startswith("```"):
            # Strip markdown code fences
            lines = clean.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            clean = "\n".join(lines).strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

# Retry parameters
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds


class LLMClient:
    """Claude API client with optional graceful degradation."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key
        self._model = model or "claude-sonnet-4-20250514"
        self._client = None

    # -- lazy init ----------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return *True* when an API key is configured."""
        return bool(self._api_key)

    def _get_client(self):  # noqa: ANN202
        if self._client is not None:
            return self._client
        if not self.available:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set — LLM features are disabled."
            )
        sdk = _ensure_sdk()
        self._client = sdk.Anthropic(api_key=self._api_key)
        return self._client

    # -- main call ----------------------------------------------------------

    def generate(
        self,
        *,
        system: str,
        user_message: str,
        max_tokens: int = 8192,
        temperature: float = 0.3,
    ) -> LLMResponse:
        """Send a single-turn message to Claude and return a wrapped response.

        Retries on transient 429/5xx errors with exponential back-off.
        """
        client = self._get_client()

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                text_parts = [
                    block.text for block in response.content if block.type == "text"
                ]
                return LLMResponse(
                    text="\n".join(text_parts),
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    stop_reason=response.stop_reason,
                )
            except Exception as exc:
                last_exc = exc
                error_name = type(exc).__name__
                # Retry on rate-limit and server errors only
                retriable = any(
                    keyword in error_name.lower()
                    for keyword in ("rate", "overloaded", "server", "timeout", "connection")
                )
                if not retriable and hasattr(exc, "status_code"):
                    retriable = getattr(exc, "status_code", 0) in {429, 500, 502, 503, 529}
                if not retriable or attempt == _MAX_RETRIES - 1:
                    raise
                wait = _RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "LLM request attempt %d failed (%s), retrying in %.1fs…",
                    attempt + 1,
                    error_name,
                    wait,
                )
                time.sleep(wait)

        # Should never reach here, but satisfy the type-checker.
        raise last_exc  # type: ignore[misc]
