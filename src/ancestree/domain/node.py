"""Node — the immutable record returned by queries, plus the recording handle.

Two classes with one deliberate split (AD10):

* ``Node`` is the frozen, hashable value object queries return. Equality
  and hash are by ``node_id``. It reads metadata and artifacts through the
  store; it cannot be written to — the 0.1.x footgun of calling
  ``add_meta`` on a queried node is now a type error.
* ``RecordingNode`` is the mutable handle ``create_node`` yields: write-side
  ``/`` (a real path into the node's transient scratch directory) and
  ``add_meta``. At block exit the store ingests everything it recorded.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 4, issue #15).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from ..ingest.workspace import NodeWorkspace
from ..util import filter_relpaths
from .metadata import PreparedEntry, prepare_entry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..store import LineageStore


@dataclass(frozen=True)
class Node:
    """One persisted pipeline step: an immutable, hashable record.

    Structural facts are attributes; ``metadata`` holds the user's entries;
    ``artifacts()`` and ``/`` return readable paths (reassembled on demand
    from the store — a node is a database row, not a directory).
    """

    node_id: str
    step_type: str = field(compare=False)
    generation: int = field(compare=False)
    parent_id: Tuple[str, ...] = field(compare=False)
    created_utc: str = field(compare=False)
    healthy: bool = field(compare=False)
    duration_s: Optional[float] = field(compare=False)
    size_bytes: int = field(compare=False)
    content_hash: Optional[str] = field(compare=False, repr=False)
    _provenance: Dict[str, Any] = field(compare=False, repr=False)
    _store: "LineageStore" = field(compare=False, repr=False)

    @property
    def metadata(self) -> Dict[str, Dict[str, Any]]:
        """The node's user metadata: each key maps to its envelope
        ``{'value', 'data_type', 'group', 'searchable'}``. Structural facts
        (step_type, generation, ...) are attributes, not metadata."""
        return self._store._metadata_envelopes(self.node_id)

    @property
    def provenance(self) -> Dict[str, Any]:
        """Who/what/how produced this node: user, python_version, platform,
        git_commit, git_dirty, git_branch."""
        return dict(self._provenance)

    def artifacts(self, contains: str = "*") -> List[Path]:
        """The node's artifact files as readable paths, reassembled from
        the store on demand (served from the session read cache).

        Args:
            contains: A glob pattern, or a plain substring matched
                case-insensitively anywhere in the filename. Defaults to
                all files.
        """
        return self._store._artifact_paths(self.node_id, contains)

    def __truediv__(self, relative: Union[str, Path]) -> Path:
        """Read-side ``/``: a readable path for one artifact.

        Raises:
            ArtifactNotFound: If the node has no artifact at that path.
        """
        return self._store._artifact_path(
            self.node_id, Path(relative).as_posix()
        )

    def __repr__(self) -> str:
        return (
            f"Node(node_id={self.node_id!r}, step_type={self.step_type!r}, "
            f"generation={self.generation})"
        )


class RecordingNode:
    """The mutable handle a ``create_node`` block writes through.

    ``node / "file.csv"`` returns a real, ready-to-write path inside the
    node's transient scratch directory; ``add_meta`` records metadata to be
    persisted at block exit. After the block closes the handle stays valid
    as a parent reference (it carries the node_id).
    """

    def __init__(
        self,
        node_id: str,
        step_type: str,
        generation: int,
        parent_id: List[str],
        workspace: NodeWorkspace,
    ) -> None:
        self.node_id = node_id
        self.step_type = step_type
        self.generation = generation
        self.parent_id = list(parent_id)
        self._workspace = workspace
        self._entries: Dict[str, PreparedEntry] = {}

    def __truediv__(self, relative: Union[str, Path]) -> Path:
        """Write-side ``/``: a ready-to-write path inside the node's
        scratch directory (intermediate directories are created).

        Raises:
            ValueError: If the path escapes the node's directory.
        """
        return self._workspace.resolve(relative)

    def add_meta(
        self,
        key: str,
        value: Any,
        group: Optional[str] = "General",
        data_type: str = "auto",
        searchable: bool = True,
    ) -> None:
        """Attaches a piece of metadata to the node.

        Searchable entries can be matched by the store's query methods;
        everything shows in the web graph. Adding an existing key
        overwrites the previous entry.

        Args:
            key: The name of the metadata entry.
            value: The value to store. Must be JSON-serialisable (numpy/
                pandas values are coerced with a warning).
            group: A heading to group related entries under in the web
                graph. Defaults to "General".
            data_type: How the value renders — 'auto' (infer), 'image',
                'link', 'table' (DataFrame), 'json', 'code', or 'text'.
            searchable: False for display-only metadata.
        """
        entry = prepare_entry(
            key, value, group=group, data_type=data_type, searchable=searchable
        )
        self._entries[key] = self._normalise_artifact_reference(entry)

    def _normalise_artifact_reference(self, entry: PreparedEntry) -> PreparedEntry:
        """Rewrites an image/link value that points into this node's
        scratch directory to the node-relative artifact path, so the
        reference stays valid once the scratch is gone and the bytes live
        in the store. URLs pass through untouched."""
        if entry.data_type not in ("image", "link"):
            return entry
        text = str(entry.value)
        if text.startswith(("http://", "https://")):
            return entry
        scratch = self._workspace.path.resolve()
        candidate = Path(text)
        if candidate.is_absolute():
            try:
                return replace(
                    entry, value=candidate.resolve().relative_to(scratch).as_posix()
                )
            except ValueError:
                return replace(entry, value=candidate.as_posix())
        return replace(entry, value=candidate.as_posix())

    def artifacts(self, contains: str = "*") -> List[Path]:
        """The files written so far, as native scratch paths — matching the
        record's ``artifacts()`` semantics so code inside the block reads
        what it just wrote at native speed."""
        files = dict(self._workspace.files())
        return [files[rel] for rel in filter_relpaths(sorted(files), contains)]

    def __repr__(self) -> str:
        return (
            f"RecordingNode(node_id={self.node_id!r}, "
            f"step_type={self.step_type!r}, generation={self.generation})"
        )
