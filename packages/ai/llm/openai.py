"""OpenAI, behind :class:`LLMProvider` (section 15.2, section 18.5).

GPT-5 mini by default. The model is asked for JSON matching a schema through
the API's structured-output mode, and the answer is validated against the
Pydantic type anyway — the vendor enforcing a schema is a convenience, not a
guarantee, and ADR-03 says the schema decides.

Uses httpx rather than the vendor SDK. Every other supplier in this codebase is
reached the same way, the call is one JSON POST, and an SDK here would add a
dependency whose release cadence we would then have to follow.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ai.llm.base import (
    InvalidStructuredOutputError,
    LLMError,
    LLMProvider,
    Usage,
)

Schema = TypeVar("Schema", bound=BaseModel)

_ENDPOINT = "https://api.openai.com/v1/chat/completions"
_CONNECT_TIMEOUT = 10.0


class OpenAIProvider(LLMProvider):
    """Structured extraction through OpenAI."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5-mini",
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 4000,
        price_per_million_in_usd: Decimal | None = None,
        price_per_million_out_usd: Decimal | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        # Left unset rather than guessed, like the transcription rate: a
        # made-up price corrupts the margin dashboard silently (ADR-09).
        self._price_in = price_per_million_in_usd
        self._price_out = price_per_million_out_usd
        self._client = client

    @property
    def name(self) -> str:
        return f"openai:{self._model}"

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(_CONNECT_TIMEOUT, read=self._timeout),
        )

    async def extract(
        self,
        system: str,
        user: str,
        schema: type[Schema],
        *,
        temperature: float = 0.1,
    ) -> tuple[Schema, Usage]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_completion_tokens": self._max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": _strict_schema(schema),
                },
            },
        }

        client = self._open()
        owned = self._client is None
        try:
            response = await client.post(_ENDPOINT, json=payload)
        except httpx.HTTPError as exc:
            message = f"could not reach OpenAI: {type(exc).__name__}"
            raise LLMError(message) from exc
        finally:
            if owned:
                await client.aclose()

        if response.status_code == 429:
            message = "OpenAI is rate limiting"
            raise LLMError(message)
        if response.status_code >= 500:
            message = f"OpenAI is failing (HTTP {response.status_code})"
            raise LLMError(message)
        if response.status_code >= 400:
            message = f"OpenAI refused the request (HTTP {response.status_code})"
            raise LLMError(message, retryable=False)

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            message = "OpenAI returned no answer"
            raise LLMError(message)

        content = choices[0].get("message", {}).get("content")
        if not content:
            # A refusal, or an answer cut off by the token ceiling. Either way
            # there is nothing to validate.
            message = "OpenAI returned an empty answer"
            raise LLMError(message)

        try:
            parsed = schema.model_validate_json(content)
        except ValidationError as exc:
            # The detail is quoted back to the model on the retry, which is
            # what makes the second attempt different from the first.
            raise InvalidStructuredOutputError(_summarise(exc)) from exc

        return parsed, self._usage(body.get("usage") or {})

    def _usage(self, reported: dict[str, Any]) -> Usage:
        tokens_in = int(reported.get("prompt_tokens", 0))
        tokens_out = int(reported.get("completion_tokens", 0))
        cost = Decimal(0)
        if self._price_in is not None and self._price_out is not None:
            million = Decimal(1_000_000)
            cost = (
                Decimal(tokens_in) / million * self._price_in
                + Decimal(tokens_out) / million * self._price_out
            ).quantize(Decimal("0.00001"))
        return Usage(model=self.name, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)


def _summarise(error: ValidationError) -> str:
    """The validation failure, without the value that caused it.

    Pydantic includes the rejected input, and here that input is meeting
    content. It must not travel into a log or a repair prompt sent to a vendor
    as an error message.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in error.errors()[:8]
    )


def _strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """A JSON Schema the strict structured-output mode accepts.

    It requires every property to be listed as required and forbids extra
    properties at every level; Pydantic marks fields with defaults optional,
    which that mode rejects outright.
    """
    document = schema.model_json_schema()
    _tighten(document)
    for definition in (document.get("$defs") or {}).values():
        _tighten(definition)
    return document


def _tighten(node: dict[str, Any]) -> None:
    if node.get("type") != "object":
        return
    properties = node.get("properties") or {}
    node["required"] = list(properties)
    node["additionalProperties"] = False
