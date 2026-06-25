# Python packages
import subprocess
import platform
import getpass
import sys
import os
from datetime import datetime, date, time
from typing import Any, Dict, Optional, Tuple
import warnings


# ---------------------------------------------------------------------------
# Metadata access & querying
#
# Reading, filtering and matching against the {kind, group, label, value}
# metadata envelopes attached to nodes.
# ---------------------------------------------------------------------------


def get_meta_val(entries: Dict[str, Any], key: str, default: Any = None) -> Any:
    e = entries.get(key)
    return e.get("value") if e else default


def is_match(meta: Dict[str, Any], **kwargs: Any) -> bool:
    """Flat key lookup against an index entry: every kwarg must equal the
    stored value, or — for callable values — return truthy when applied to it."""
    for key, value in kwargs.items():
        stored = meta.get(key)
        if callable(value):
            try:
                if not value(stored):
                    return False
            except Exception as e:
                warnings.warn(
                    f"Predicate for {key!r} raised {type(e).__name__}: {e}. "
                    "Node treated as non-matching.",
                    UserWarning,
                    stacklevel=5,
                )
                return False
        elif stored != value:
            return False
    return True


def flatten_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: v.get("value")
        for k, v in meta.items()
        if isinstance(v, dict) and v.get("searchable", True)
    }


def is_pandas(obj: Any) -> bool:
    """
    Sniffs an object's metadata to determine if it is a pandas DataFrame instance.

    Args:
        obj: Any python object.

    Returns:
        bool: Returns True if the object is a pandas DataFrame.
    """
    return type(obj).__name__ == "DataFrame" and hasattr(obj, "to_dict")


def to_jsonable(value: Any) -> Tuple[Any, bool]:
    """Best-effort coercion of a value into JSON-serialisable Python types.

    Data workflows routinely hand the store numpy/pandas values — `df["x"].sum()`
    is a ``numpy.int64``, `df["x"].mean()` a ``numpy.float64`` — which `json`
    cannot serialise (``np.float64`` happens to subclass ``float`` and slips
    through; ``np.int64`` does not). This walks the value, converting numpy
    scalars/arrays, datetimes/Timestamps, and sets/tuples to native Python, and
    recursing through dicts and lists.

    numpy and pandas are optional dependencies, so detection is by duck typing
    (mirroring `is_pandas`) — neither is imported here.

    Args:
        value: Any value passed to `add_meta`.

    Returns:
        Tuple[Any, bool]: ``(converted, changed)`` where ``changed`` is True if
            anything was coerced (so the caller can warn). Values that are
            already JSON-native are returned untouched with ``changed=False``.
    """
    changed = False

    def convert(v: Any) -> Any:
        nonlocal changed
        # JSON-native scalars (bool/int/float/str) pass straight through. Note
        # numpy.float64 subclasses float and lands here untouched.
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        # datetimes — pandas Timestamp subclasses datetime.datetime, so this
        # catches it too.
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
        if hasattr(v, "tolist") and getattr(v, "ndim", 0):
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
        # Unrecognised: hand it back unchanged for the caller's serialisability
        # check to reject with a clear, call-site error.
        return v

    return convert(value), changed


# ---------------------------------------------------------------------------
# Time formatting & parsing
#
# Turning ISO timestamp strings into display text or datetime objects.
# ---------------------------------------------------------------------------


def parse_time(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %Y, %H:%M:%S")
    except ValueError:
        return iso_str


def parse_iso_utc(s: str) -> datetime:
    """
    Returns a datetime from a string.

    Args:
        s (str): String object representing a datetime.

    Returns:
        datetime: A datetime object.
    """
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Provenance capture
#
# Recording who / what / how produced a node: user, environment and git state.
# ---------------------------------------------------------------------------


def safe_get_user() -> str:
    """
    Attempt to retrieve the users credentials
    """
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")


def get_environment_provenance() -> Dict[str, str]:
    """
    Track who and what produced the node.
    """
    return {
        "user": safe_get_user(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }


def _git_output(*args: str) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, encoding="utf-8"
        ).strip()
    except Exception:
        # Not a git repo or git not installed
        return None


def get_git_provenance() -> Dict[str, Any]:
    """
    Track the git state the node was produced under.
    """
    status = _git_output("status", "--porcelain")
    return {
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_dirty": bool(status),
        "git_branch": _git_output("rev-parse", "--abbrev-ref", "HEAD"),
    }


def get_provenance() -> Dict[str, Any]:
    """
    Track who/ what/ how produced the node. Returns a flat dict so each
    field can be stored as an individual metadata entry via add_meta.
    """
    return {**get_environment_provenance(), **get_git_provenance()}
