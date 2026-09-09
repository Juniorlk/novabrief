"""Turning database rows into response models.

`GET /me` and the two `PATCH` endpoints return the same shapes, and a client
that saw a profile change form between the read and the write would have to
handle two versions of the same object. Building them in one place is what
keeps that from happening.

Presenters only read. Anything that decides something belongs in a service.
"""

from __future__ import annotations

from app.models import Organization, User
from schemas.auth import CurrentSession, OrganizationProfile, UserProfile


def user_profile(user: User) -> UserProfile:
    """A user as the API returns them. No password hash, no TOTP secret."""
    return UserProfile(
        id=user.id,
        email=user.email,
        phone=user.phone,
        full_name=user.full_name,
        role=user.role,  # type: ignore[arg-type]
        locale=user.locale,  # type: ignore[arg-type]
        timezone=user.timezone,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
    )


def organization_profile(organization: Organization) -> OrganizationProfile:
    """An organization as the API returns it."""
    return OrganizationProfile(
        id=organization.id,
        name=organization.name,
        legal_id=organization.legal_id,
        market=organization.market,
        plan_code=organization.plan_code,
        default_language=organization.default_language,
        audio_retention_days=organization.audio_retention_days,
        quota_seconds=organization.quota_seconds,
        consumed_seconds=organization.consumed_seconds,
        status=organization.status,
        lexicon=organization.lexicon,
        created_at=organization.created_at,
    )


def current_session(*, user: User, organization: Organization) -> CurrentSession:
    """The pair every `/me`-shaped response returns."""
    return CurrentSession(
        user=user_profile(user),
        organization=organization_profile(organization),
    )
