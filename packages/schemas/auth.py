"""Request and response shapes for accounts and organizations.

These Pydantic models are the source of truth for the contract (ADR-10): the
TypeScript types the web app and the desktop client use are generated from
them, so the three cannot drift apart.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# EF-01: phone numbers are stored in E.164, which is unambiguous across the
# countries NovaBrief will open in and is what Mobile Money expects.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

MIN_PASSWORD_LENGTH = 10

Role = Literal["OWNER", "ADMIN", "MEMBER"]
Locale = Literal["fr", "en"]

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)]


class _Base(BaseModel):
    """Shared configuration: reject unknown fields."""

    # A typo in a client payload should be a visible 422, not a value silently
    # ignored while the caller believes it was applied.
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_Base):
    """EF-01: create an account and its organization in one step."""

    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str = Field(min_length=1, max_length=200)
    password: Password

    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)

    locale: Locale = "fr"
    timezone: str = Field(default="Africa/Douala", max_length=64)

    @field_validator("phone")
    @classmethod
    def _phone_is_e164(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().replace(" ", "")
        if not _E164.match(candidate):
            message = "phone must be in E.164 format, for example +237690000000"
            raise ValueError(message)
        return candidate

    @model_validator(mode="after")
    def _needs_one_identifier(self) -> RegisterRequest:
        if self.email is None and self.phone is None:
            message = "either email or phone is required"
            raise ValueError(message)
        return self


class LoginRequest(_Base):
    """Sign in with whichever identifier the account was created with."""

    password: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _needs_one_identifier(self) -> LoginRequest:
        if self.email is None and self.phone is None:
            message = "either email or phone is required"
            raise ValueError(message)
        return self


class RefreshRequest(_Base):
    """Exchange a refresh token for a new pair."""

    refresh_token: str = Field(min_length=1, max_length=512)


class TokenPair(_Base):
    """What a successful authentication returns.

    The refresh token is opaque and single-use: presenting it returns a new
    pair, and presenting it a second time revokes the whole family.
    """

    access_token: str
    refresh_token: str
    # Not a secret: this is the OAuth 2.0 token *type*, which the client
    # echoes in the Authorization header.
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105
    expires_in: int = Field(description="Access token lifetime in seconds.")


class UserProfile(_Base):
    """EF-04: the profile the web and desktop apps display."""

    id: uuid.UUID
    email: EmailStr | None
    phone: str | None
    full_name: str
    role: Role
    locale: Locale
    timezone: str
    email_verified: bool
    created_at: datetime


class OrganizationProfile(_Base):
    """EF-05: organization settings."""

    id: uuid.UUID
    name: str
    legal_id: str | None
    market: str
    plan_code: str
    default_language: str
    audio_retention_days: int
    quota_seconds: int
    consumed_seconds: int
    status: str
    # Proper nouns and acronyms handed to the transcription provider as
    # keyterms, which is what makes local names come back spelled correctly.
    lexicon: list[str]
    created_at: datetime


class CurrentSession(_Base):
    """`GET /me`: who is signed in, and where."""

    user: UserProfile
    organization: OrganizationProfile


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "CurrentSession",
    "LoginRequest",
    "OrganizationProfile",
    "RefreshRequest",
    "RegisterRequest",
    "Role",
    "TokenPair",
    "UserProfile",
]
