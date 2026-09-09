"""LLM doubles.

The validation campaign was cancelled on 2026-09-08, so nothing here calls a
model. These return what a test told them to return, or fail the way a test
told them to fail — including the failure that matters most, an answer the
schema rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ai.llm.base import InvalidStructuredOutputError, LLMError, Usage

Schema = TypeVar("Schema", bound=BaseModel)


@dataclass
class FakeLLMProvider:
    """Answers with whatever it was configured to answer."""

    provider_name: str = "fake:v1"
    # Successive raw answers. A string is validated like a real one, so a test
    # can hand it something malformed and watch the repair loop work.
    answers: list[str] = field(default_factory=list)
    failures: list[LLMError] = field(default_factory=list)
    usage: Usage = field(
        default_factory=lambda: Usage(
            model="fake:v1", tokens_in=1000, tokens_out=200, cost_usd=Decimal("0.00100")
        )
    )
    calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.provider_name

    async def extract(
        self,
        system: str,
        user: str,
        schema: type[Schema],
        *,
        temperature: float = 0.1,
    ) -> tuple[Schema, Usage]:
        self.calls.append((system, user))

        if self.failures:
            raise self.failures.pop(0)
        if not self.answers:
            message = "the double was given no answer to return"
            raise LLMError(message, retryable=False)

        raw = self.answers.pop(0)
        try:
            return schema.model_validate_json(raw), self.usage
        except ValidationError as exc:
            raise InvalidStructuredOutputError(str(exc.error_count())) from exc
