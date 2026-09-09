"""Turning database rows into response models.

`GET /me` and the two `PATCH` endpoints return the same shapes, and a client
that saw a profile change form between the read and the write would have to
handle two versions of the same object. Building them in one place is what
keeps that from happening.

Presenters only read. Anything that decides something belongs in a service.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import Decision, Meeting, Organization, Report, Task, TranscriptSegment, User
from app.services.organizations import ExportBundle
from schemas.auth import (
    CurrentSession,
    ExportedAuditEntry,
    ExportedInvitation,
    ExportedMember,
    OrganizationExport,
    OrganizationProfile,
    UserProfile,
)
from schemas.meetings import (
    DecisionOut,
    MeetingSummary,
    ReportOut,
    TaskOut,
    TranscriptSegmentOut,
)


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
        deletion_requested_at=organization.deletion_requested_at,
        created_at=organization.created_at,
    )


def current_session(*, user: User, organization: Organization) -> CurrentSession:
    """The pair every `/me`-shaped response returns."""
    return CurrentSession(
        user=user_profile(user),
        organization=organization_profile(organization),
    )


def organization_export(bundle: ExportBundle) -> OrganizationExport:
    """EF-06: the export document.

    Every field is named one by one rather than copied from the row. That is
    the point of writing it out: a column added later — a TOTP secret, a
    recovery code, a provider token — does not silently appear in a file the
    customer downloads and forwards. Password hashes, token hashes and TOTP
    secrets exist on these rows and none of them is listed below.
    """
    return OrganizationExport(
        exported_at=datetime.now(UTC),
        organization=organization_profile(bundle.organization),
        members=[
            ExportedMember(
                id=member.id,
                email=member.email,
                phone=member.phone,
                full_name=member.full_name,
                role=member.role,  # type: ignore[arg-type]
                locale=member.locale,
                timezone=member.timezone,
                email_verified=member.email_verified_at is not None,
                revoked=member.revoked_at is not None,
                created_at=member.created_at,
            )
            for member in bundle.members
        ],
        invitations=[
            ExportedInvitation(
                id=invitation.id,
                email=invitation.email,
                role=invitation.role,  # type: ignore[arg-type]
                invited_by=invitation.invited_by,
                expires_at=invitation.expires_at,
                accepted_at=invitation.accepted_at,
                revoked_at=invitation.revoked_at,
                created_at=invitation.created_at,
            )
            for invitation in bundle.invitations
        ],
        audit_log=[
            ExportedAuditEntry(
                id=entry.id,
                actor_id=entry.actor_id,
                actor_type=entry.actor_type,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                ip=entry.ip,
                reason=entry.reason,
                metadata=entry.metadata_json,
                created_at=entry.created_at,
            )
            for entry in bundle.audit_entries
        ],
    )


def meeting_summary(meeting: Meeting) -> MeetingSummary:
    """A meeting as the API returns it.

    The storage key is absent on purpose: it names an object in a private
    bucket and belongs in a presigned URL, not in a listing.
    """
    return MeetingSummary(
        id=meeting.id,
        title=meeting.title,
        status=meeting.status,  # type: ignore[arg-type]
        language=meeting.language,
        started_at=meeting.started_at,
        duration_seconds=meeting.duration_seconds,
        is_private=meeting.is_private,
        debug_id=meeting.debug_id,
        created_by=meeting.created_by,
        failed_reason=meeting.failed_reason,
        purge_at=meeting.purge_at,
        created_at=meeting.created_at,
        completed_at=meeting.completed_at,
    )


def transcript_segment(segment: TranscriptSegment) -> TranscriptSegmentOut:
    """One diarised passage."""
    return TranscriptSegmentOut(
        id=segment.id,
        speaker_tag=segment.speaker_tag,
        speaker_name=segment.speaker_name,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        text=segment.text,
        confidence=segment.confidence,
        channel=segment.channel,
    )


def report(row: Report, *, decisions: list[Decision], tasks: list[Task]) -> ReportOut:
    """The structured report and everything extracted from it."""
    return ReportOut(
        id=row.id,
        title=row.title,
        participants=list(row.participants or []),
        summary=list(row.summary or []),
        decisions=[
            DecisionOut(
                id=decision.id,
                content=decision.content,
                source_start_ms=decision.source_start_ms,
                confidence=decision.confidence,
                human_status=decision.human_status,
                edited_content=decision.edited_content,
            )
            for decision in decisions
        ],
        tasks=[
            TaskOut(
                id=task.id,
                action=task.action,
                assignee_name=task.assignee_name,
                assignee_user_id=task.assignee_user_id,
                deadline_text=task.deadline_text,
                deadline_date=task.deadline_date,
                source_start_ms=task.source_start_ms,
                confidence=task.confidence,
                human_status=task.human_status,
                edited_content=task.edited_content,
            )
            for task in tasks
        ],
        model_version=row.model_version,
        prompt_version=row.prompt_version,
        generated_at=row.generated_at,
    )
