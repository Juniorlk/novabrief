"""UUID version 7, as specified by RFC 9562.

Section 19.2 asks for sortable primary keys. A v7 identifier carries a
millisecond timestamp in its leading bits, so rows created in sequence land in
sequence on disk. That matters for a table like `meetings`, which is almost
always read newest-first: a random v4 key scatters inserts across the B-tree
and turns a page of recent rows into a page of random reads.

The value stays unguessable, which section 21.1 requires against enumeration:
74 random bits remain, and the timestamp prefix reveals only when a row was
created — never how many exist or which identifier comes next.

`uuid.uuid7` is standard from Python 3.14; this implementation exists because
the project targets 3.12.
"""

from __future__ import annotations

import os
import time
import uuid

# Layout (RFC 9562 section 5.7), 128 bits total:
#   48 bits  unix_ts_ms
#    4 bits  version (7)
#   12 bits  rand_a
#    2 bits  variant (0b10)
#   62 bits  rand_b
_VERSION = 7
_VARIANT_RFC4122 = 0b10


def uuid7(*, timestamp_ms: int | None = None) -> uuid.UUID:
    """Return a new UUID v7.

    `timestamp_ms` is for tests that need a fixed instant; leave it unset in
    application code.
    """
    milliseconds = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000

    # 48 bits of timestamp. Masking rather than asserting keeps the function
    # total: a clock beyond year 10889 wraps instead of raising in production.
    value = (milliseconds & 0xFFFF_FFFF_FFFF) << 80

    random_bits = int.from_bytes(os.urandom(10), "big")  # 80 bits available
    value |= (_VERSION & 0xF) << 76
    value |= ((random_bits >> 64) & 0xFFF) << 64  # rand_a
    value |= (_VARIANT_RFC4122 & 0b11) << 62
    value |= random_bits & 0x3FFF_FFFF_FFFF_FFFF  # rand_b

    return uuid.UUID(int=value)


def timestamp_ms_of(value: uuid.UUID) -> int:
    """Read back the creation time embedded in a v7 identifier."""
    if value.version != _VERSION:
        message = f"expected a UUID v7, got v{value.version}"
        raise ValueError(message)
    return value.int >> 80
