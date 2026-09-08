"""Sending transactional email, behind a provider (ADR-01).

Resend is the choice for V1 (section 15.2), but no business code imports it:
everything goes through :class:`EmailProvider`, so swapping it is configuration
rather than a rewrite. The same reasoning as the transcription and billing
providers, applied to a smaller supplier.

Three implementations:

* :class:`ResendProvider` — production.
* :class:`ConsoleProvider` — development, prints the message. A developer
  without an API key still sees the invitation link.
* :class:`RecordingProvider` — tests, keeps messages in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.logging import get_logger

logger = get_logger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Message:
    """One transactional email.

    Both a text and an HTML body: some corporate clients strip HTML, and an
    invitation nobody can open is an invitation that did not happen.
    """

    to: str
    subject: str
    text: str
    html: str | None = None


class EmailDeliveryError(Exception):
    """The provider refused or could not be reached."""


class EmailProvider(Protocol):
    """Sends transactional email."""

    async def send(self, message: Message) -> None:
        """Deliver one message, or raise :class:`EmailDeliveryError`."""
        ...


class ResendProvider:
    """Delivery through Resend (section 15.2)."""

    def __init__(self, *, api_key: str, sender: str, reply_to: str | None = None) -> None:
        self._api_key = api_key
        self._sender = sender
        self._reply_to = reply_to

    async def send(self, message: Message) -> None:
        payload: dict[str, object] = {
            "from": self._sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html
        if self._reply_to:
            payload["reply_to"] = self._reply_to

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    _RESEND_ENDPOINT,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as exc:
            message_text = f"could not reach the email provider: {type(exc).__name__}"
            raise EmailDeliveryError(message_text) from exc

        if response.status_code >= 400:
            # The provider's body is not included: it echoes the payload, which
            # holds the recipient address and the message.
            failure = f"the email provider refused the message (HTTP {response.status_code})"
            raise EmailDeliveryError(failure)


class ConsoleProvider:
    """Prints instead of sending, for development without an API key."""

    async def send(self, message: Message) -> None:
        # This is the one place meeting-adjacent text is printed on purpose,
        # and it only ever runs in development.
        logger.info(
            "email_not_sent_console_provider",
            to=message.to,
            subject=message.subject,
        )
        print(f"\n--- email to {message.to} ---\n{message.subject}\n\n{message.text}\n---\n")  # noqa: T201


@dataclass
class RecordingProvider:
    """Keeps messages in memory so tests can read what was sent."""

    sent: list[Message] = field(default_factory=list)
    fail_next: bool = False

    async def send(self, message: Message) -> None:
        if self.fail_next:
            self.fail_next = False
            raise EmailDeliveryError("simulated delivery failure")
        self.sent.append(message)

    def last_to(self, address: str) -> Message | None:
        """The most recent message sent to an address, if any."""
        for message in reversed(self.sent):
            if message.to == address:
                return message
        return None
