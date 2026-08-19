"""Tests for the UUIDv7 generator (time-ordered correlation IDs)."""

from __future__ import annotations

import time
import uuid


def test_uuid7_returns_valid_uuid():
    from approvalflow.idgen import uuid7

    value = uuid7()

    assert isinstance(value, uuid.UUID)


def test_uuid7_sets_version_and_variant_bits():
    from approvalflow.idgen import uuid7

    value = uuid7()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_is_time_ordered():
    from approvalflow.idgen import uuid7

    first = uuid7()
    time.sleep(0.01)
    second = uuid7()

    assert str(first) < str(second)


def test_uuid7_generates_unique_values():
    from approvalflow.idgen import uuid7

    values = {uuid7() for _ in range(1000)}

    assert len(values) == 1000
