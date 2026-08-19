"""UUIDv7 generation (RFC 9562) — time-ordered IDs.

Pure stdlib implementation: no new dependency, and the ordering property
improves index locality for correlation_id-keyed queries on the SQL audit
tables (ingestion.submissions, router.decisions, etc.).
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7: 48-bit millisecond timestamp + 74 random bits."""
    unix_ms = int(time.time() * 1000)
    rand = os.urandom(10)

    time_bytes = unix_ms.to_bytes(6, byteorder="big")
    # rand[0] holds rand_a (12 bits); top nibble is overwritten with the version.
    rand_a_hi = (0x7 << 4) | (rand[0] & 0x0F)
    # rand[2] holds the variant bits (top 2 bits = 10) in rand_b.
    rand_b_hi = (0x80 | (rand[2] & 0x3F))

    value = time_bytes + bytes([rand_a_hi, rand[1], rand_b_hi]) + rand[3:10]
    return uuid.UUID(bytes=value)
