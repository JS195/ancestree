"""Phase 2: util coercion/time helpers and provenance capture (issue #13)."""

import sys
from datetime import datetime

from ancestree.domain.provenance import capture
from ancestree.util import format_timestamp, parse_iso_utc, to_jsonable


def test_to_jsonable_passes_native_values_untouched() -> None:
    value = {"a": [1, 2.5, "x", True, None]}
    converted, changed = to_jsonable(value)
    assert converted == value and changed is False


def test_to_jsonable_coerces_common_types() -> None:
    converted, changed = to_jsonable((1, 2))
    assert converted == [1, 2] and changed is True

    converted, changed = to_jsonable({1: "a"})
    assert converted == {"1": "a"} and changed is True

    converted, changed = to_jsonable({"s": {3}})
    assert converted == {"s": [3]} and changed is True

    stamp = datetime(2026, 7, 8, 12, 30)  # noqa: DTZ001 - naive on purpose
    converted, changed = to_jsonable(stamp)
    assert converted == stamp.isoformat() and changed is True


def test_time_helpers_roundtrip_and_degrade() -> None:
    iso = "2026-07-08T03:04:05"
    assert parse_iso_utc(iso) == datetime(2026, 7, 8, 3, 4, 5)  # noqa: DTZ001
    assert format_timestamp(iso) == "08 Jul 2026, 03:04:05"
    assert format_timestamp(None) == "N/A"
    assert format_timestamp("not-a-date") == "not-a-date"


def test_capture_returns_the_full_record() -> None:
    record = capture()
    assert set(record) == {
        "user",
        "python_version",
        "platform",
        "git_commit",
        "git_dirty",
        "git_branch",
    }
    assert isinstance(record["user"], str) and record["user"]
    assert record["python_version"] == sys.version.split()[0]
    assert isinstance(record["git_dirty"], bool)
