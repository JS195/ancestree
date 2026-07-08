"""The metadata envelope: validation, coercion and type inference.

``prepare_entry`` turns a user's ``add_meta`` call into a validated
``PreparedEntry`` — reserved-key guard, ``auto`` type inference,
DataFrame/json coercion, numpy/pandas-to-native conversion, and the
serialisability check that fails loudly at the call site rather than at
block exit. Pure logic, no I/O; the persistence encoding (canonical JSON +
``num_value``) lives in db/metadata_store.py.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 4, issue #15).
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Optional

from ..util import is_pandas, to_jsonable

VALID_DATA_TYPES: FrozenSet[str] = frozenset(
    {"auto", "image", "link", "table", "json", "code", "text"}
)

#: Keys the store owns — the structural columns (and their 0.1.x aliases),
#: refused by add_meta so user metadata can never shadow a structural fact
#: in queries.
RESERVED_KEYS: FrozenSet[str] = frozenset(
    {
        "node_id",
        "parent_id",
        "step_type",
        "generation",
        "healthy",
        "timestamp",
        "created_utc",
        "created_epoch",
        "duration_s",
        "size_bytes",
        "size_mb",
        "content_hash",
    }
)

#: Path suffixes rendered inline as images when the data_type is inferred.
IMAGE_SUFFIXES: FrozenSet[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
)


@dataclass(frozen=True)
class PreparedEntry:
    """One validated metadata entry, ready to persist."""

    key: str
    value: Any
    data_type: str
    group: Optional[str]
    searchable: bool


def infer_data_type(value: Any) -> str:
    """Maps a value to its natural data_type for 'auto' mode. Only types
    that are unambiguous signals are inferred: DataFrames, dicts/lists,
    Path objects (a Path is a deliberate file reference, never prose), and
    URL strings. Plain strings always stay 'text' — sniffing string
    contents is how auto-detection produces surprises."""
    if is_pandas(value):
        return "table"
    if isinstance(value, (dict, list)):
        return "json"
    if isinstance(value, Path):
        return "image" if value.suffix.lower() in IMAGE_SUFFIXES else "link"
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return "link"
    return "text"


def prepare_entry(
    key: str,
    value: Any,
    group: Optional[str] = "General",
    data_type: str = "auto",
    searchable: bool = True,
) -> PreparedEntry:
    """Validates and coerces one ``add_meta`` call into a PreparedEntry.

    Raises:
        ValueError: For a reserved key or an unknown data_type.
        TypeError: For a 'table' that is not a DataFrame, a 'json' that is
            not a dict/list, or a value that is not JSON-serialisable even
            after coercion.
    """
    if key in RESERVED_KEYS:
        raise ValueError(
            f"{key!r} is a reserved key set by the store and cannot be "
            "set via add_meta."
        )
    if data_type not in VALID_DATA_TYPES:
        raise ValueError(
            f"Invalid data_type {data_type!r}. "
            f"Must be one of: {', '.join(sorted(VALID_DATA_TYPES))}"
        )

    if data_type == "auto":
        data_type = infer_data_type(value)

    if data_type in ("image", "link"):
        value = str(value)
        # File references are display-only; URLs stay searchable. The
        # node-relative path normalisation happens in the recording handle,
        # which knows the scratch directory.
        if not value.startswith(("http://", "https://")):
            searchable = False

    if data_type == "table":
        if not is_pandas(value):
            raise TypeError(
                f"Expected a pandas DataFrame for 'table', got "
                f"{type(value).__name__}"
            )
        split = value.to_dict(orient="split")
        value = {"columns": split["columns"], "rows": split["data"]}
        searchable = False

    if data_type == "json":
        if not isinstance(value, (dict, list)):
            raise TypeError(
                f"Expected a dict or list for 'json', got "
                f"{type(value).__name__}"
            )
        searchable = False

    # Coerce numpy/pandas and other common non-JSON types to native Python
    # so the eventual write cannot fail, and warn that we did. Anything
    # still not serialisable is rejected here, at the call site, rather
    # than at block exit where the traceback would not point at add_meta.
    value, coerced = to_jsonable(value)
    if coerced:
        warnings.warn(
            f"Metadata {key!r} held values that are not natively JSON-"
            "serialisable (e.g. numpy/pandas scalars, arrays, sets, "
            "datetimes); they were coerced to plain Python types. Pass "
            "native Python types to silence this.",
            UserWarning,
            stacklevel=3,
        )
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"Metadata {key!r} is not JSON-serialisable even after "
            f"coercion ({type(exc).__name__}: {exc}). Convert it to plain "
            "Python types (int/float/str/bool/list/dict) before calling "
            "add_meta."
        ) from None

    return PreparedEntry(
        key=key,
        value=value,
        data_type=data_type,
        group=group,
        searchable=searchable,
    )
