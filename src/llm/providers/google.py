"""Google GenAI LLM provider wrapping the ``google-genai`` SDK.

Uses ``google.genai.Client`` with ``client.aio.models.generate_content()``
for asynchronous content generation.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from src.core.enums import LLMProviderType, MessageRole
from src.core.exceptions import ProviderError
from src.core.models import LLMMessage, LLMResponse, Tool, ToolCall
from src.llm.base import LLMProvider

if TYPE_CHECKING:
    from src.config import Settings

logger = structlog.get_logger(__name__)

# Per-million-token pricing: (input, output)
_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "gemini-3.1-pro-preview": (Decimal("2.00"), Decimal("15.00")),
    "gemini-3-flash-preview": (Decimal("0.50"), Decimal("3.00")),
    "gemini-3.1-flash-lite-preview": (Decimal("0.25"), Decimal("1.50")),
}


class GoogleProvider(LLMProvider):
    """Google GenAI LLM provider (Gemini models)."""

    def __init__(self, *, model_id: str, settings: Settings) -> None:
        self._model_id = model_id
        self._settings = settings
        self._client: genai.Client | None = None
        self._initialized = False

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> LLMProviderType:
        return LLMProviderType.GOOGLE

    @property
    def model_id(self) -> str:
        return self._model_id

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if not self._settings.GOOGLE_API_KEY:
            raise ProviderError("GOOGLE_API_KEY is not set")
        self._client = genai.Client(api_key=self._settings.GOOGLE_API_KEY)
        self._initialized = True

    async def shutdown(self) -> None:
        self._client = None
        self._initialized = False

    async def health_check(self) -> bool:
        return self._initialized

    # ── Core LLM ──────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[Tool] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        start = time.monotonic()
        system_instruction, contents = _convert_messages(messages)

        config_kwargs: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            config_kwargs["max_output_tokens"] = max_tokens
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if tools:
            config_kwargs["tools"] = _convert_tools(tools)
        config = types.GenerateContentConfig(**config_kwargs)

        response = await self._call_with_retry(
            lambda: self._client.aio.models.generate_content(  # type: ignore[union-attr]
                model=self._model_id,
                contents=contents,
                config=config,
            ),
        )

        # Extract content
        try:
            content = response.text or ""
        except Exception:
            content = ""

        # Extract tool calls
        tool_calls: list[ToolCall] = []
        if response.candidates and response.candidates[0].content:
            for i, part in enumerate(response.candidates[0].content.parts or []):
                if part.function_call:
                    tool_calls.append(
                        ToolCall(
                            id=f"google_tc_{i}",
                            name=part.function_call.name,
                            arguments=dict(part.function_call.args) if part.function_call.args else {},
                        )
                    )

        # Token counts
        tokens_in = tokens_out = 0
        if response.usage_metadata:
            tokens_in = response.usage_metadata.prompt_token_count or 0
            tokens_out = response.usage_metadata.candidates_token_count or 0
        if not tokens_in:
            tokens_in = self._estimate_tokens(str(messages))
        if not tokens_out:
            tokens_out = self._estimate_tokens(content)

        finish_reason = "stop"
        if tool_calls:
            finish_reason = "tool_calls"

        return LLMResponse(
            content=content,
            model=self._model_id,
            provider=self.provider_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_calculate_cost(self._model_id, tokens_in, tokens_out),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    async def structured_output(
        self,
        messages: list[LLMMessage],
        schema: type[BaseModel],
        *,
        temperature: float = 0.3,
    ) -> tuple[BaseModel, int, int, Decimal]:
        start = time.monotonic()
        system_instruction, contents = _convert_messages(messages)

        # Gemini API doesn't support additionalProperties in JSON schemas.
        # Generate schema from Pydantic, strip unsupported fields, then pass as dict.
        json_schema = _strip_additional_properties(schema.model_json_schema())

        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "response_mime_type": "application/json",
            "response_schema": json_schema,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        config = types.GenerateContentConfig(**config_kwargs)

        response = await self._call_with_retry(
            lambda: self._client.aio.models.generate_content(  # type: ignore[union-attr]
                model=self._model_id,
                contents=contents,
                config=config,
            ),
        )

        tokens_in = tokens_out = 0
        if response.usage_metadata:
            tokens_in = response.usage_metadata.prompt_token_count or 0
            tokens_out = response.usage_metadata.candidates_token_count or 0
        if not tokens_in:
            tokens_in = self._estimate_tokens(str(messages))
        if not tokens_out:
            tokens_out = self._estimate_tokens(response.text or "")
        cost = _calculate_cost(self._model_id, tokens_in, tokens_out)

        try:
            text = response.text or ""
            return schema.model_validate_json(text), tokens_in, tokens_out, cost
        except Exception as exc:
            raise ProviderError(f"Failed to parse structured output: {exc}") from exc

    # ── Retry logic ───────────────────────────────────────────────────

    async def _call_with_retry(self, coro_factory: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._settings.LLM_MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except genai_errors.ClientError as exc:
                status = getattr(exc, "code", None)
                if status in (401, 403):
                    raise ProviderError(f"Google auth error: {exc}") from exc
                if status == 429:
                    last_exc = exc
                    if attempt < self._settings.LLM_MAX_RETRIES:
                        await asyncio.sleep(2**attempt)
                        continue
                    # Retries exhausted for rate limit — fall through to final raise
                else:
                    raise ProviderError(f"Google API client error: {exc}") from exc
            except genai_errors.ServerError as exc:
                last_exc = exc
                if attempt < self._settings.LLM_MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
            except Exception as exc:
                raise ProviderError(f"Google unexpected error: {exc}") from exc

        raise ProviderError(f"Google max retries exceeded: {last_exc}") from last_exc


# ── Conversion helpers (module-level) ─────────────────────────────────


def _strip_additional_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove ``additionalProperties`` from a JSON schema.

    Gemini API rejects schemas containing this keyword.  Also inline
    any ``$defs`` references so the schema is fully self-contained.
    """
    defs = schema.pop("$defs", None) or {}

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            # Resolve $ref pointers
            if "$ref" in node:
                ref_path = node["$ref"]  # e.g. "#/$defs/SentimentResult"
                ref_name = ref_path.rsplit("/", 1)[-1]
                if ref_name in defs:
                    return _resolve(defs[ref_name])
                return node

            node.pop("additionalProperties", None)
            # title is also unsupported in some Gemini schema contexts
            node.pop("title", None)

            # Handle anyOf (Pydantic Optional fields) → pick first non-null
            if "anyOf" in node:
                variants = [v for v in node["anyOf"] if v.get("type") != "null"]
                if len(variants) == 1:
                    resolved = _resolve(variants[0])
                    # Preserve description if present
                    desc = node.get("description")
                    if desc and isinstance(resolved, dict):
                        resolved.setdefault("description", desc)
                    return resolved

            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    return _resolve(schema)  # type: ignore[return-value]


_ROLE_MAP = {
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "model",
    MessageRole.TOOL: "user",
}


def _convert_messages(
    messages: list[LLMMessage],
) -> tuple[str | None, list[types.Content]]:
    """Convert LLMMessage list to Google GenAI format.

    Returns (system_instruction, contents). System messages are extracted
    into the system_instruction string; remaining messages become Content objects.
    """
    system_parts: list[str] = []
    contents: list[types.Content] = []

    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            system_parts.append(msg.content)
            continue
        role = _ROLE_MAP.get(msg.role, "user")
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg.content)],
            )
        )

    system_instruction = "\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def _convert_tools(tools: list[Tool]) -> list[types.Tool]:
    """Convert Tool list to Google GenAI tool format."""
    declarations = []
    for tool in tools:
        properties = {}
        required = []
        for param in tool.parameters:
            prop: dict[str, Any] = {"type": param.type.upper(), "description": param.description}
            if param.enum:
                prop["enum"] = param.enum
            properties[param.name] = prop
            if param.required:
                required.append(param.name)
        declarations.append(
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        k: types.Schema(**v) for k, v in properties.items()
                    },
                    required=required,
                ),
            )
        )
    return [types.Tool(function_declarations=declarations)]


def _calculate_cost(model_id: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Calculate cost based on pricing table."""
    rates = _PRICING.get(model_id)
    if rates is None:
        logger.warning("unknown_model_pricing", model_id=model_id)
        return Decimal("0")
    return rates[0] * tokens_in / 1_000_000 + rates[1] * tokens_out / 1_000_000
