# Python packages
import subprocess
import platform
import getpass
import sys
import os
from datetime import datetime
from typing import Any, Dict, Optional
import warnings


# ---------------------------------------------------------------------------
# Metadata access & querying
#
# Reading, filtering and matching against the {kind, group, label, value}
# metadata envelopes attached to nodes.
# ---------------------------------------------------------------------------


def get_meta_val(
    entries: Dict[str, Any], key: str, default: Any = None
) -> Any:
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
