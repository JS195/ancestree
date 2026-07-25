"""Small shared helpers: JSON coercion and ISO time parsing/formatting.

``to_jsonable`` duck-types numpy/pandas values into JSON-native Python so
both stay optional dependencies. The two time helpers are the one home for
timestamp handling (retiring the 0.1.x ``parse_time``/``parse_iso_utc``
split).

See REBUILD_BLUEPRINT.md section 5.3 (Phase 2, issue #13).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time
from pathlib import PurePosixPath
from typing import Any


def to_jsonable(value: Any) -> tuple[Any, bool]:
    """Best-effort coercion of a value into JSON-serialisable Python types.

    Data workflows routinely hand the store numpy/pandas values —
    ``df["x"].sum()`` is a ``numpy.int64``, ``df["x"].mean()`` a
    ``numpy.float64`` — which ``json`` cannot serialise. This walks the
    value, converting numpy scalars/arrays, datetimes/Timestamps, and
    sets/tuples to native Python, recursing through dicts and lists.

    numpy and pandas are optional dependencies, so detection is by duck
    typing — neither is imported here.

    Args:
        value: Any value destined for a JSON column.

    Returns:
        Tuple[Any, bool]: ``(converted, changed)`` where ``changed`` is True
            if anything was coerced (so the caller can warn). Values that
            are already JSON-native come back untouched with
            ``changed=False``.
    """
    changed = False

    def convert(v: Any) -> Any:
        nonlocal changed
        # JSON-native scalars (bool/int/float/str) pass straight through.
        # Note numpy.float64 subclasses float and lands here untouched.
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        # datetimes — pandas Timestamp subclasses datetime.datetime, so
        # this catches it too.
        if isinstance(v, (datetime, date, time)):
            changed = True
            return v.isoformat()
        if isinstance(v, dict):
            out = {}
            for k, val in v.items():
                key = k if isinstance(k, str) else str(k)
                if key is not k:
                    changed = True
                out[key] = convert(val)
            return out
        if isinstance(v, (list, tuple, set, frozenset)):
            if not isinstance(v, list):
                changed = True
            return [convert(x) for x in v]
        # numpy ndarray (or anything array-like with a positive ndim).
        if hasattr(v, "tolist") and hasattr(v, "ndim") and v.ndim:
            changed = True
            return convert(v.tolist())
        # numpy scalar: a 0-d value carrying a dtype.
        if hasattr(v, "item") and hasattr(v, "dtype"):
            changed = True
            return convert(v.item())
        # Anything else exposing isoformat (e.g. pandas Timedelta).
        if hasattr(v, "isoformat"):
            changed = True
            return v.isoformat()
        # Unrecognised: hand it back unchanged for the caller's
        # serialisability check to reject with a clear, call-site error.
        return v

    return convert(value), changed


def parse_iso_utc(s: str) -> datetime:
    """Parses an ISO-8601 timestamp string into a datetime."""
    return datetime.fromisoformat(s)


def format_timestamp(iso_str: str | None) -> str:
    """Renders an ISO timestamp for display (``02 Jan 2026, 03:04:05``).

    Returns ``"N/A"`` for a missing value and the raw string for one that
    does not parse — display code should never raise over a timestamp.
    """
    if not iso_str:
        return "N/A"
    try:
        return datetime.fromisoformat(iso_str).strftime("%d %b %Y, %H:%M:%S")
    except ValueError:
        return iso_str


def is_pandas(obj: Any) -> bool:
    """True when `obj` walks and quacks like a pandas DataFrame. Detection
    is by duck typing so pandas stays an optional dependency."""
    return type(obj).__name__ == "DataFrame" and hasattr(obj, "to_dict")


def filter_relpaths(relpaths: Iterable[str], contains: str = "*") -> list[str]:
    """The artifact-name matcher shared by every artifacts() surface: a
    pattern with wildcards is a glob; anything else also matches as a
    case-insensitive substring of the filename (0.1.x semantics)."""
    pattern = contains if ("*" in contains or "?" in contains) else f"*{contains}*"
    lowered = contains.lower()
    matched: list[str] = []
    for relpath in relpaths:
        name = relpath.rsplit("/", 1)[-1]
        if PurePosixPath(relpath).match(pattern) or lowered in name.lower():
            matched.append(relpath)
    return matched
