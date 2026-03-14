"""Abstract base class for LLM providers.

Every LLM backend (OpenAI, Anthropic, Google) implements this ABC.
Follows the ``DataProvider`` lifecycle pattern with LLM-specific methods.

- ``initialize/shutdown/health_check`` — lifecycle (abstract).
- ``chat`` — send messages, optionally with tools.
- ``structured_output`` — parse response into a Pydantic model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from src.core.enums import LLMProviderType

if TYPE_CHECKING:
    from pydantic import BaseModel

    from src.core.models import LLMMessage, LLMResponse, Tool


class LLMProvider(ABC):
    """Abstract LLM provider with lifecycle and core LLM methods."""

    # ── Identity ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def provider_name(self) -> LLMProviderType:
        """LLM provider type (e.g. ``LLMProviderType.OPENAI``)."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Model identifier (e.g. ``"gpt-5.4"``, ``"claude-opus-4-6"``)."""

    # ── Lifecycle (abstract) ──────────────────────────────────────────

    @abstractmethod
    async def initialize(self) -> None:
        """Establish connections and validate credentials."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources and close connections."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return ``True`` if the provider is ready to serve requests."""

    # ── Core LLM (abstract) ───────────────────────────────────────────

    @abstractmethod
    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[Tool] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send messages to the LLM and return a response.

        Args:
            messages: Conversation history.
            tools: Optional function-calling tool definitions.
            temperature: Sampling temperature (0.0–2.0).
            max_tokens: Maximum tokens in the response.
        """

    @abstractmethod
    async def structured_output(
        self,
        messages: list[LLMMessage],
        schema: type[BaseModel],
        *,
        temperature: float = 0.3,
    ) -> BaseModel:
        """Send messages and parse the response into a Pydantic model.

        Args:
            messages: Conversation history.
            schema: Target Pydantic model class.
            temperature: Sampling temperature (lower for precision).
        """

    # ── Concrete helpers ──────────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate: ~4 chars per token."""
        return max(len(text) // 4, 1)

    # ── Context manager ───────────────────────────────────────────────

    async def __aenter__(self) -> LLMProvider:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.shutdown()
