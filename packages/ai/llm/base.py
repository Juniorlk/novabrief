"""The LLM contract (ADR-01, ADR-03, section 18.1).

Business code names no model and no vendor. It asks for a Pydantic type and
gets an instance of it, or an error — never free text, and never a "best
effort" object with fields the schema does not describe.

The audio never reaches here. ADR-02 draws that line: a model sees the
transcript, never the recording, which is what makes every extracted item
traceable to a timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, TypeVar

from pydantic import BaseModel

Schema = TypeVar("Schema", bound=BaseModel)


@dataclass(frozen=True)
class Usage:
    """What one call consumed, for `usage_ledger` (ADR-08)."""

    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal = Decimal(0)


class LLMError(Exception):
    """The model failed or refused."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)


class InvalidStructuredOutputError(LLMError):
    """The answer did not match the schema (ADR-03).

    Kept separate from a transport failure because the response is different:
    a network error is retried as-is, while this one is retried with the
    validation error quoted back, which is what actually changes the answer.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"the model returned something the schema rejects: {detail}")


class LLMProvider(Protocol):
    """Produces a validated object from a prompt."""

    @property
    def name(self) -> str:
        """Vendor and model, written to the ledger."""
        ...

    async def extract(
        self,
        system: str,
        user: str,
        schema: type[Schema],
        *,
        temperature: float = 0.1,
    ) -> tuple[Schema, Usage]:
        """Answer as an instance of `schema`, or raise.

        Temperature is low by default and configurable rather than fixed: this
        is an extraction task, and creativity here is called hallucination.
        """
        ...
