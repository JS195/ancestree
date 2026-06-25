# Python packages
import hashlib
import json
import os
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Dict, Union, Optional, Literal
from copy import deepcopy

# Internal dependancies
from .chunkstore import ChunkStore, chunk_bytes, get_read_cache
from .utils import flatten_meta, get_provenance, is_pandas, to_jsonable

# Reassembling a packed artifact reads and decompresses its chunks; both release
# the GIL, so a small thread pool overlaps that work for large files. Threads are
# only worth their overhead past a chunk count, and ~4 workers is the sweet spot
# (more contends on the GIL for the Python-level glue), regardless of core count.
_MATERIALIZE_THREAD_THRESHOLD = 32
_MATERIALIZE_WORKERS = min(4, os.cpu_count() or 1)

#: Per-node sidecar holding the recipe for every packed artifact. Excluded from
#: artifact listings and content hashing — it describes artifacts, it is not one.
ARTIFACT_MANIFEST = ".artifacts.json"

_DataType = Literal["auto", "image", "link", "table", "json", "code", "text"]
_VALID_DATA_TYPES = {"auto", "image", "link", "table", "json", "code", "text"}
_RESERVED_KEYS = {
    "parent_id",
    "step_type",
    "generation",
    "healthy",
    "timestamp",
    "duration_s",
    "size_mb",
    "content_hash",
}

# Provenance keys written by Node._create. They record *who/where/when* a node
# was produced, not *what* it contains, so they are excluded from the content
# fingerprint used for deduplication.
_PROVENANCE_KEYS = {
    "user",
    "python_version",
    "platform",
    "git_commit",
    "git_dirty",
    "git_branch",
}

# Metadata keys that do not contribute to a node's content identity: structural
# fields (handled separately or derived), the content hash itself, and
# provenance. Everything else — user-added metadata — is content.
_NON_CONTENT_KEYS = _RESERVED_KEYS | _PROVENANCE_KEYS | {"node_id"}


class Node:
    """
    Represents a single step in the pipeline: a directory on disk holding the step's artifacts and a meta.json describing it.

    Nodes are not constructed directly. They are created by `LineageStore.create_node` and returned by the store's search and lineage methods (`find_node`, `get_lineage`, `get_child_nodes`, ...). Interact with a node to read and attach metadata, locate its artifacts, and build paths inside its directory with the `/` operator.

    Attributes:
        path (Path): The node's directory on disk.
        node_id (str): The unique 8-character identifier of the node.
        generation (int): The generation number of the node in the pipeline.
        parent_id (str): The node_id of the parent node, or None for a root node.
        step_type (str): The type of pipeline step this node represents.
    """

    def __init__(
        self,
        path: Path,
        node_id: str,
        generation: int,
        parent_id: Optional[str] = None,
        step_type: Optional[str] = None,
    ):
        """
        Initialises a node instance. Performs no I/O: use Node._load() to read
        an existing node from disk or Node._create() to initialise a new one.

        Args:
            path (Path): The filesystem path where the node is stored.
            node_id (str): A unique 8-character alphanumeric identifier.
            generation (int): The generation number of the node in the pipeline.
            parent_id (str): The unique 8-character identifier of node the current node descends from.
        """
        self.path = path
        self.node_id = node_id
        self.generation = generation
        self.parent_id = parent_id
        self.step_type = step_type
        self._metadata: Optional[Dict[str, Any]] = {}
        self._system_keys: set[Any] = set()
        # Lazily loaded recipe (relpath -> {size, sha256, chunks}) for artifacts
        # that have been packed into the chunk store. Empty when nothing is packed.
        self._manifest_data: Optional[Dict[str, Any]] = None

    @property
    def metadata(self) -> Dict[str, Dict[str, Any]]:
        """
        The node's full metadata as a dictionary.

        Each key maps to an entry of the form `{'value': ..., 'type': ..., 'group': ..., 'searchable': ...}`. This includes both the structural metadata written by the store (node_id, parent_id, generation, step_type, timestamp, provenance) and anything added with `add_meta`.

        Returns a deep copy: mutating the result does not change the node. Use `add_meta` to modify metadata.

        Returns:
            Dict[str, Dict]: A mapping of metadata key to its entry dictionary.

        Examples:
            >>> node.metadata["accuracy"]["value"]
            0.92
        """
        return deepcopy(self._hydrate())

    def _hydrate(self) -> Dict[str, Any]:
        # Index-backed nodes (_from_index) defer reading meta.json until the
        # full metadata is actually needed.
        if self._metadata is None:
            self._metadata = json.loads((self.path / "meta.json").read_text())
        assert self._metadata is not None
        return self._metadata

    @classmethod
    def _from_index(cls, path: Path, flat: Dict[str, Any]) -> "Node":
        """
        Builds a node from the database's flattened index entry without
        touching disk. The full metadata is hydrated lazily from meta.json
        on first access.

        Args:
            path (Path): The directory of the node.
            flat (dict): The node's flat index entry (key -> value).

        Returns:
            Node: The index-backed node.
        """
        node = cls(
            path,
            flat.get("node_id", ""),
            flat.get("generation", 0),
            flat.get("parent_id"),
            step_type=flat.get("step_type"),
        )
        node._metadata = None
        return node

    @classmethod
    def _load(cls, path: Path) -> "Node":
        """
        Loads an existing node from its meta.json, which is the single source
        of truth for the structural attributes.

        Args:
            path (Path): The directory of the node to load.

        Returns:
            Node: The loaded node.
        """
        meta = json.loads((path / "meta.json").read_text())

        def _value(key: str) -> Any:
            return meta.get(key).get("value")

        node = cls(
            path,
            _value("node_id"),
            _value("generation"),
            _value("parent_id"),
            step_type=_value("step_type"),
        )
        node._metadata = meta
        return node

    @classmethod
    def _create(
        cls,
        path: Path,
        node_id: str,
        generation: int,
        parent_id: Optional[str] = None,
        step_type: Optional[str] = None,
    ) -> "Node":
        """
        Initialises a brand new node with its structural and provenance
        metadata. The initial keys are recorded in _system_keys so the store
        can tell system-written metadata apart from user additions.

        Args:
            path (Path): The filesystem path where the node is stored.
            node_id (str): A unique 8-character alphanumeric identifier.
            generation (int): The generation number of the node in the pipeline.
            parent_id (str): The unique 8-character identifier of node the current node descends from.

        Returns:
            Node: The new node. Nothing is written to disk until _write_meta().
        """
        node = cls(path, node_id, generation, parent_id, step_type=step_type)
        node._set_meta(
            "node_id", node_id, data_type="text", group="Structural Properties"
        )
        node._set_meta(
            "parent_id", parent_id, data_type="text", group="Structural Properties"
        )
        node._set_meta(
            "generation", generation, data_type="text", group="Structural Properties"
        )
        node._set_meta(
            "step_type", step_type, data_type="text", group="Structural Properties"
        )
        node._set_meta(
            "timestamp",
            datetime.now(timezone.utc).isoformat(),
            data_type="text",
            group="Structural Properties",
        )
        for key, value in get_provenance().items():
            node._set_meta(
                key, value, data_type="text", group="Provenance", searchable=False
            )
        node._system_keys = set(node._hydrate())
        return node

    #: Path suffixes rendered inline as images when the data_type is inferred.
    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

    @staticmethod
    def _infer_data_type(value: Any) -> _DataType:
        """
        Maps a value to its natural data_type for 'auto' mode. Only types
        that are unambiguous signals are inferred: DataFrames, dicts/lists,
        Path objects (a Path is a deliberate file reference, never prose),
        and URL strings. Plain strings always stay 'text' — sniffing string
        contents is how auto-detection produces surprises.
        """
        if is_pandas(value):
            return "table"
        if isinstance(value, (dict, list)):
            return "json"
        if isinstance(value, Path):
            return "image" if value.suffix.lower() in Node.IMAGE_SUFFIXES else "link"
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return "link"
        return "text"

    def add_meta(
        self,
        key: str,
        value: Any,
        group: Optional[str] = "General",
        data_type: _DataType = "auto",
        searchable: bool = True,
    ) -> None:
        """
        Attaches a piece of metadata to the node.

        Metadata is what makes a node discoverable: every searchable entry can be matched by the store's search methods (`find_node`, `find_in_lineage`, `get_most_recent_node`) and is displayed in the interactive web graph. Adding a key that already exists overwrites the previous entry.

        Args:
            key (str): The name of the metadata entry.
            value (Any): The value to store. Must be JSON-serialisable.
            group (str, optional): A heading to group related entries under in the web graph display. Defaults to General.
            data_type (str, optional): How the value is rendered in the web graph. The default 'auto' infers it from the value: pandas DataFrames render as tables, dicts and lists as formatted JSON, Path objects as inline images (image suffixes) or file links, http(s) strings as links, and everything else as plain text. Pass a type explicitly to override the inference: 'image' for a path to an image file inside the node (the value is rewritten relative to the store root), 'link' for a clickable link, 'table' for a pandas DataFrame (stored as columns/rows), 'json' for a dict or list, 'code' for a monospaced snippet (e.g. the SQL or shell command that produced the node), or 'text' for plain text.
            searchable (bool, optional): If True the entry is indexed and can be matched by the store's search methods. Set to False for display-only metadata. Defaults to True.

        Examples:
            >>> with store.create_node(step_type="model", parent=clean_node) as node:
            ...     # Plain values — searchable by default
            ...     node.add_meta("accuracy", 0.94, group="Metrics")
            ...     node.add_meta("epochs", 42, group="Metrics")
            ...     node.add_meta("learning_rate", 1e-3, group="Config")
            ...
            ...     # Dict/list — rendered as formatted JSON in the web graph
            ...     node.add_meta("params", {"optimizer": "adam", "dropout": 0.3}, group="Config")
            ...
            ...     # Table — pandas DataFrame rendered as a sortable table; not searchable
            ...     node.add_meta("results", df, data_type="table", group="Outputs")
            ...
            ...     # Image — path rewritten relative to store root, rendered inline
            ...     fig.savefig(node / "confusion.png")
            ...     node.add_meta("confusion_matrix", node / "confusion.png", group="Figures")
            ...
            ...     # Code — rendered in a monospaced block
            ...     node.add_meta("query", "SELECT * FROM runs WHERE status = 'ok'", data_type="code")
            ...
            ...     # Display-only — visible in the web graph but excluded from find_node
            ...     node.add_meta("notes", "rerun after fixing label encoding", searchable=False)
            ...
            ...     # External link — rendered as a clickable URL
            ...     node.add_meta("wandb_run", "https://wandb.ai/my-org/run/abc123", group="Links")
        """
        if key in _RESERVED_KEYS:
            raise ValueError(
                f"{key!r} is a reserved key set by the store and cannot be "
                "set via add_meta."
            )
        self._set_meta(
            key, value, group=group, data_type=data_type, searchable=searchable
        )

    def _set_meta(
        self,
        key: str,
        value: Any,
        group: Optional[str] = "General",
        data_type: _DataType = "auto",
        searchable: bool = True,
    ) -> None:
        """Write a metadata entry, bypassing the reserved-key guard.

        The store calls this to set its own structural and provenance keys —
        the ones `add_meta` refuses from users. Validation, type inference and
        coercion are shared; only the guard is not.
        """
        if data_type not in _VALID_DATA_TYPES:
            raise ValueError(
                f"Invalid data_type {data_type!r}. "
                f"Must be one of: {', '.join(sorted(_VALID_DATA_TYPES))}"
            )

        if data_type == "auto":
            data_type = self._infer_data_type(value)

        if data_type in ("image", "link") and not str(value).startswith(
            ("http://", "https://")
        ):
            # Store file references relative to the store root so they
            # resolve from the generated HTML. URLs pass through untouched —
            # Path() would collapse their double slash.
            value = str(
                Path(
                    str(value)
                    .removeprefix(str(self.path.parent) + "/")
                    .removeprefix(str(self.path.parent))
                )
            )
            searchable = False

        if data_type == "table":
            if not is_pandas(value):
                raise TypeError(
                    f"Expected a pandas DataFrame for 'table', got {type(value).__name__}"
                )
            split = value.to_dict(orient="split")
            value = {"columns": split["columns"], "rows": split["data"]}
            searchable = False

        if data_type == "json":
            if not isinstance(value, (dict, list)):
                raise TypeError(
                    f"Expected a dict or list for 'json', got {type(value).__name__}"
                )
            searchable = False

        # Coerce numpy/pandas and other common non-JSON types to native Python so
        # the eventual meta.json write cannot fail, and warn that we did. Anything
        # still not serialisable is rejected here, at the call site, rather than
        # at block exit where the traceback would not point at this add_meta.
        value, coerced = to_jsonable(value)
        if coerced:
            warnings.warn(
                f"Metadata {key!r} held values that are not natively JSON-"
                "serialisable (e.g. numpy/pandas scalars, arrays, sets, "
                "datetimes); they were coerced to plain Python types. Pass native "
                "Python types to silence this.",
                UserWarning,
                stacklevel=3,
            )
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"Metadata {key!r} is not JSON-serialisable even after coercion "
                f"({type(exc).__name__}: {exc}). Convert it to plain Python types "
                "(int/float/str/bool/list/dict) before calling add_meta."
            ) from None

        entry = {
            f"{key}": {
                "value": value,
                "data_type": data_type,
                "group": group,
                "searchable": searchable,
            }
        }

        self._hydrate().update(entry)

    def _write_meta(self) -> None:
        """
        Internal helper for creating and writing metadata atomically to prevent corruption during crashes.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        # Atomic write
        try:
            temp_file = self.path / "meta.json.tmp"
            temp_file.write_text(json.dumps(self.metadata, indent=2))
            temp_file.replace(self.path / "meta.json")
        finally:
            if temp_file.exists():
                temp_file.unlink()

    def _content_meta(self) -> Dict[str, Any]:
        """The node's user-supplied metadata entries — everything that is not
        a structural, provenance, or content-hash key. This is the metadata
        portion of the node's content identity."""
        return {
            key: self._normalise_entry(entry)
            for key, entry in self._hydrate().items()
            if key not in _NON_CONTENT_KEYS
        }

    def _normalise_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Strips this node's id out of file-referencing metadata so identical
        content fingerprints the same. An ``image``/``link`` value that points
        at a file inside the node is stored with the (random) node_id as a path
        segment; left in, it would make every run look unique. URLs are real
        content and pass through untouched. Returns a copy when rewritten; the
        node's own metadata is never mutated."""
        if entry.get("data_type") not in ("image", "link"):
            return entry
        value = entry.get("value")
        if not isinstance(value, str) or value.startswith(("http://", "https://")):
            return entry
        parts = Path(value).parts
        if self.node_id not in parts:
            return entry
        rest = parts[parts.index(self.node_id) + 1 :]
        return {**entry, "value": Path(*rest).as_posix() if rest else ""}

    def _artifact_digests(self) -> Dict[str, str]:
        """Maps each artifact's path (relative to the node's *own* directory,
        so it is independent of the random node_id) to the SHA-256 of its
        bytes. For packed artifacts the digest is read straight from the recipe,
        so no reassembly is needed. meta.json and the manifest are excluded —
        they describe the node, they are not its content."""
        manifest = self._manifest()
        digests: Dict[str, str] = {
            rel: record["sha256"] for rel, record in manifest.items()
        }
        for f in sorted(self.path.rglob("*")):
            if f.is_file() and f.name not in ("meta.json", ARTIFACT_MANIFEST):
                rel = f.relative_to(self.path).as_posix()
                digests.setdefault(rel, hashlib.sha256(f.read_bytes()).hexdigest())
        return digests

    def content_hash(self) -> str:
        """A SHA-256 fingerprint of the node's content: its step_type, parent,
        user metadata, and the bytes of every artifact. Excludes volatile
        fields (node_id, timestamp, duration, size, healthy flag) and
        provenance, so two runs producing the same step with the same metadata
        and identical artifact bytes share a fingerprint.

        Used by the store to deduplicate nodes when `dedupe=True`. The hash is
        only a fast bucket key — the store byte-verifies a candidate match with
        `_content_equal` before reusing it.

        Returns:
            str: The hex SHA-256 digest.
        """
        payload = {
            "step_type": self.step_type,
            "parent_id": self.parent_id,
            "meta": self._content_meta(),
            "artifacts": self._artifact_digests(),
        }
        blob = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _content_equal(self, other: "Node") -> bool:
        """True if this node and `other` are content-identical: same step_type,
        same parent, same user metadata, and byte-identical artifacts. This is
        the definitive check behind deduplication — the content hash only
        narrows the candidates; equality is confirmed here so a hash collision
        can never cause two genuinely different nodes to be merged."""
        return (
            self.step_type == other.step_type
            and self.parent_id == other.parent_id
            and self._content_meta() == other._content_meta()
            and self._artifact_digests() == other._artifact_digests()
        )

    def to_db(self) -> Dict[str, Any]:
        # Flat {searchable key: value} dict for indexing/searching. Shares the
        # one definition of "searchable flattening" with the database, which
        # applies it to raw meta.json dicts on reconcile/rebuild.
        return flatten_meta(self._hydrate())

    def artifacts(self, contains: str = "*") -> List[Path]:
        """
        Searches this node's directory returning all files excluding internal metadata.
        Recursively finds all artifacts regardless of storage depth.

        Args:
            contains (str, optional): A glob pattern to filter discovered files. A plain substring also works: it is matched anywhere in the filename, case-insensitively. Defaults to "*" (all files).

        Returns:
            List[Path]: The matching file paths, ready to read or pass to a
                loader directly (no need to prefix the store root). A loose file
                points inside the node's directory; a packed artifact points at a
                reassembled copy in the store's read cache. Either way the path
                holds readable bytes, mirroring what `node / "file"` returns.

        Examples:
            >>> node.artifacts("*.csv")
            [PosixPath('/data/store/abc12345/sample.csv')]
            >>> pd.read_csv(node.artifacts("sample")[0])
        """
        search_pattern = contains
        if "*" not in contains and "?" not in contains:
            search_pattern = f"*{contains}*"

        artifacts = []
        for rel in sorted(self._artifact_rels()):
            name = rel.rsplit("/", 1)[-1]
            if Path(rel).match(search_pattern) or name.lower().find(
                contains.lower()
            ) != -1:
                artifacts.append(self._resolve(rel))
        return artifacts

    def _artifact_rels(self) -> set:
        """The node's logical artifact paths (relative to the node): the union of
        packed manifest entries and loose files actually on disk. meta.json and
        the manifest are excluded — they describe the node, they are not its
        content. This is the storage-independent view artifact listing and the
        web graph build on, since a packed artifact has no file in the node dir."""
        rels = set(self._manifest())
        for f in self.path.rglob("*"):
            if f.is_file() and f.name not in ("meta.json", ARTIFACT_MANIFEST):
                rels.add(f.relative_to(self.path).as_posix())
        return rels

    def _resolve(self, rel: str) -> Path:
        """Returns a readable filesystem path for the logical artifact `rel`.

        Prefers the loose file when it is still on disk — that is a native read,
        no decompression — which is the common case for artifacts written this
        session before the background packer has reclaimed them. Once the loose
        file is gone the artifact is served from its chunks via the read cache;
        the manifest is reloaded first in case it was packed after we cached it.
        Because a loose file is only ever removed once its recipe is durable, a
        missing loose file guarantees the manifest can rebuild it."""
        loose = self.path / rel
        if loose.exists():
            return loose
        if rel not in self._manifest():
            self._manifest_data = None  # may have been packed since we cached it
        if rel in self._manifest():
            return self._materialize(rel)
        return loose

    def __truediv__(self, relative_loc: Union[Path, str]) -> Path:
        """
        Allows the use of the '/' operator to create paths inside the node's directory, mirroring pathlib.
        This is the idiomatic way to choose where to write an artifact. Any intermediate directories in the path are created automatically, so the returned path is always ready to write to.

        Args:
            relative_loc (Union[Path, str]): The string or Path object to append to the node's base path.

        Returns:
            Path: The resolved destination inside the node's directory.

        Raises:
            ValueError: If the path escapes the node's directory.

        Examples:
            >>> with store.create_node(step_type="clean") as node:
            ...     df.to_csv(node / "results/cleaned.csv")
        """
        target_path = (self.path / relative_loc).resolve()
        if not target_path.is_relative_to(self.path.resolve()):
            raise ValueError(
                f"Artifact path {relative_loc!r} escapes the node directory. "
                "Keep all artifact paths inside the node."
            )
        # A packed artifact with no loose file on disk is a read: reassemble it
        # into the read cache and return that path (reading is transparent). A
        # loose file (read) or a not-yet-written path (write target) returns a
        # path inside the node directory.
        rel = target_path.relative_to(self.path.resolve()).as_posix()
        if not target_path.exists():
            if rel not in self._manifest():
                self._manifest_data = None  # may have been packed in the background
            if rel in self._manifest():
                return self._materialize(rel)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        return target_path

    # ---------------------------------------------------------------------------
    # Chunked storage (manifest, packing, reassembly)
    # ---------------------------------------------------------------------------

    def _manifest(self) -> Dict[str, Any]:
        """The recipe for every packed artifact, loaded lazily and cached.
        Empty when nothing in this node is packed."""
        if self._manifest_data is None:
            path = self.path / ARTIFACT_MANIFEST
            self._manifest_data = (
                json.loads(path.read_text()) if path.exists() else {}
            )
        return self._manifest_data

    def _write_manifest(self, manifest: Dict[str, Any]) -> None:
        self._manifest_data = manifest
        path = self.path / ARTIFACT_MANIFEST
        tmp = self.path / f"{ARTIFACT_MANIFEST}.tmp"
        try:
            tmp.write_text(json.dumps(manifest, indent=2))
            tmp.replace(path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _pack(self) -> None:
        """Chunks every loose artifact file into the shared pool and records the
        recipe in the manifest. This only ever ADDS — it writes content-addressed
        chunks (idempotent: an already-present chunk is a no-op) and then
        atomically replaces the manifest. It never deletes a loose file; that is
        done separately by `_reclaim_loose` at a quiescent point.

        Because of that ordering it is safe to interrupt at any instant (Ctrl+C,
        crash, kill): an artifact is always recoverable from its loose file (here
        until reclaimed) AND, once this returns, from its chunks — there is never
        a moment where neither exists. A half-finished run leaves only orphan
        chunks (reclaimed later by `gc`) and an unchanged manifest."""
        store = ChunkStore(self.path.parent)
        manifest = dict(self._manifest())
        changed = False
        for f in sorted(self.path.rglob("*")):
            if not f.is_file() or f.name in ("meta.json", ARTIFACT_MANIFEST):
                continue
            if f.name.endswith(".tmp"):
                continue
            rel = f.relative_to(self.path).as_posix()
            if rel in manifest:
                continue  # already chunked (artifacts are immutable)
            data = f.read_bytes()
            manifest[rel] = {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "chunks": [store.put(chunk) for chunk in chunk_bytes(data)],
            }
            changed = True
        if changed:
            self._write_manifest(manifest)  # durable before any loose file is removed

    def _reclaim_loose(self) -> None:
        """Removes loose artifact files whose recipe is already durably in the
        manifest, reclaiming the space the chunk pool now holds. Call ONLY at a
        quiescent point (no reader may be holding a loose path) — `flush`/close
        do. Safe to interrupt: every file removed is reproducible from its
        chunks, so a partial run just leaves more to reclaim next time."""
        manifest = self._manifest()
        if not manifest:
            return
        for f in sorted(self.path.rglob("*")):
            if not f.is_file() or f.name in ("meta.json", ARTIFACT_MANIFEST):
                continue
            if f.name.endswith(".tmp"):
                continue
            rel = f.relative_to(self.path).as_posix()
            if rel in manifest:
                f.unlink(missing_ok=True)

    def _materialize(self, rel: str) -> Path:
        """Reassembles a packed artifact from its chunks into the store's read
        cache and returns that path. A cached copy from earlier in the session is
        reused as-is; otherwise the chunks are read and decompressed (in parallel
        for large files), verified against the recorded SHA-256, and written
        atomically. The node directory is never touched, so packed nodes stay
        packed without a manual `compact`."""
        record = self._manifest()[rel]
        out = get_read_cache(self.path.parent).path_for(self.node_id, rel)
        if out.exists():
            return out  # already reassembled this session

        store = ChunkStore(self.path.parent)
        digests = record["chunks"]
        if len(digests) >= _MATERIALIZE_THREAD_THRESHOLD and _MATERIALIZE_WORKERS > 1:
            # store.get reads + zlib-decompresses each chunk, both releasing the
            # GIL, so the pool overlaps real work. map() keeps chunk order.
            with ThreadPoolExecutor(max_workers=_MATERIALIZE_WORKERS) as pool:
                data = b"".join(pool.map(store.get, digests, chunksize=16))
        else:
            data = b"".join(store.get(h) for h in digests)

        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise RuntimeError(
                f"Artifact {rel!r} failed its integrity check while being "
                "reassembled — the chunk store may be corrupt."
            )
        # Atomic publish: a unique temp keeps concurrent reassembles of the same
        # entry from clobbering each other mid-write.
        tmp = out.with_name(f"{out.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        return out

    def __repr__(self) -> str:
        """
        Returns a developer friendly string representation of the node.

        Examples:
            >>> node = store.get_node("abc12345")
            >>> print(node)
            'Node = abc12345, path = abc12345, generation = 0'
        """
        return f"Node = {self.node_id}, path = {self.path.name}, generation = {self.generation}"
