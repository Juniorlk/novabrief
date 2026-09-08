"""UUID v7 must be well-formed, sortable, and not guessable."""

from __future__ import annotations

import uuid

import pytest

from app.uuid7 import timestamp_ms_of, uuid7


def test_version_and_variant_follow_the_rfc() -> None:
    value = uuid7()

    assert value.version == 7
    # RFC 9562 variant bits are 0b10, which occupy the two high bits of the
    # clock_seq_hi_variant byte.
    assert (value.int >> 62) & 0b11 == 0b10


def test_identifiers_sort_by_creation_time() -> None:
    """The reason for choosing v7 over v4 (section 19.2)."""
    early = uuid7(timestamp_ms=1_700_000_000_000)
    late = uuid7(timestamp_ms=1_700_000_001_000)

    assert early < late
    assert str(early) < str(late)


def test_the_timestamp_survives_a_round_trip() -> None:
    moment = 1_757_260_800_000
    value = uuid7(timestamp_ms=moment)

    assert timestamp_ms_of(value) == moment


def test_identifiers_minted_in_the_same_millisecond_still_differ() -> None:
    """74 random bits remain, so collisions are not a practical concern."""
    moment = 1_700_000_000_000
    values = {uuid7(timestamp_ms=moment) for _ in range(1_000)}

    assert len(values) == 1_000


def test_identifiers_are_not_sequential() -> None:
    """Section 21.1 requires resources not be enumerable.

    Two identifiers from the same millisecond must not differ by a small step,
    or an attacker who holds one could walk to its neighbours.
    """
    moment = 1_700_000_000_000
    first = uuid7(timestamp_ms=moment)
    second = uuid7(timestamp_ms=moment)

    assert abs(first.int - second.int) > 2**32


def test_reading_the_timestamp_of_another_version_is_refused() -> None:
    with pytest.raises(ValueError, match="expected a UUID v7"):
        timestamp_ms_of(uuid.uuid4())
