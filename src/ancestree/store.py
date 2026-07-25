"""LineageStore — the public facade.

Wires ConnectionManager, MetadataStore, ChunkStore and RuleEngine together
and exposes the public API: ``create_node``, the query vocabulary
(``get``/``find``/``latest``/``lineage``/``children``/``ancestors``/
``from_parent``), the power tools (``sql``, ``stats``) and the store
lifecycle. Orchestration only — every algorithm lives in a focused module.

Store policy (``rules``, ``gen_triggers``, ``dedup``, ``chunk``) is
persisted in the database at creation and cannot be changed afterwards;
reopening with different values warns and uses the stored policy.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 4, issue #15).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union
import weakref

from .db.chunk_store import ChunkStore
from .db.connection import ConnectionManager
from .db.metadata_store import MetadataStore, NodeRecord, metadata_row
from .domain.fingerprint import ContentSummary
from .domain.node import Node, RecordingNode
from .domain.provenance import capture
from .domain.rules import RuleEngine, validate_step_type
from .ingest.packing import ingest_node
from .ingest.workspace import NodeWorkspace
from .maintenance import Pruner, compact_chunks, sweep_orphan_scratch
from .util import filter_relpaths
from .web.export import export_static

DB_FILENAME = "ancestree.db"

#: Anything the public API accepts where a node is expected.
NodeLike = Union[str, Node, RecordingNode, None]
#: A create_node parent: one node-like, or a list/tuple of them (a join).
ParentArg = Union[NodeLike, List[NodeLike], Tuple[NodeLike, ...]]


class LineageStore:
    """Orchestrates the lineage and interactions of a data pipeline.

    The entire durable store is one SQLite database (``<root>/ancestree.db``)
    holding metadata and deduplicated artifact chunks. A store can be
    recreated from its root at any time; rules and policy persist in the
    database itself.
    """

    def __init__(
        self,
        root: Union[Path, str],
        rules: Optional[Dict[str, List[str]]] = None,
        gen_triggers: Optional[List[str]] = None,
        dedup: Optional[bool] = None,
        chunk: Optional[bool] = None,
    ) -> None:
        """Opens (creating if needed) the store at `root`.

        Args:
            root: The store's directory; the database lives inside it.
            rules: Allowed transitions, e.g. ``{"clean": ["ingest"]}``.
                Persisted at creation; immutable afterwards.
            gen_triggers: Step types that start a new generation.
                Persisted at creation; immutable afterwards.
            dedup: Node-level deduplication policy (persisted at creation;
                defaults to True). Enforced from Phase 5.
            chunk: Layer-2 (delta) dedup policy (persisted at creation;
                defaults to True). Enforced from Phase 6.
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._manager = ConnectionManager(self.root / DB_FILENAME)
        self._metadata = MetadataStore(self._manager)
        config = self._load_or_create_config(rules, gen_triggers, dedup, chunk)
        self.rules: Dict[str, List[str]] = config["rules"]
        self.gen_triggers: List[str] = config["gen_triggers"]
        self.dedup: bool = config["dedup"]
        self.chunk: bool = config["chunk"]
        # The chunk policy is Layer-2 (resemblance/delta) storage (AD5).
        self._chunks = ChunkStore(self._manager, delta=self.chunk)
        self._engine = RuleEngine(self.rules, self.gen_triggers)
        self._pruner = Pruner(self._manager, self._metadata)
        # Resources are released when close() is called, when the store is
        # garbage collected, or at interpreter exit — whichever comes first.
        # The finalizer callback must not reference `self` (that would keep
        # the store alive forever), so the resources travel in this dict.
        self._resources: Dict[str, Any] = {
            "manager": self._manager,
            "chunks": self._chunks,
            "live_server": None,
        }
        self._finalizer = weakref.finalize(
            self, LineageStore._release, self._resources
        )
        adopted = sweep_orphan_scratch(
            self.root, self._manager, self._metadata, self._chunks
        )
        if adopted:
            warnings.warn(
                f"Adopted {len(adopted)} orphaned node(s) from a crashed "
                f"session as unhealthy: {adopted}. Their partial artifacts "
                "are preserved as evidence.",
                UserWarning,
                stacklevel=2,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "LineageStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Releases the store's connections and wipes the session read
        cache. Idempotent; reopen by constructing a new LineageStore.
        Runs automatically when the store is garbage collected or the
        interpreter exits, so calling it explicitly is optional."""
        self._finalizer()

    @staticmethod
    def _release(resources: Dict[str, Any]) -> None:
        server = resources.pop("live_server", None)
        if server is not None:
            server.close()
        resources["chunks"].clear_cache()
        resources["manager"].close()

    def _load_or_create_config(
        self,
        rules: Optional[Dict[str, List[str]]],
        gen_triggers: Optional[List[str]],
        dedup: Optional[bool],
        chunk: Optional[bool],
    ) -> Dict[str, Any]:
        row = self._manager.read().execute(
            "SELECT value FROM config WHERE key = 'policy'"
        ).fetchone()
        if row is None:
            stored: Dict[str, Any] = {
                "rules": rules or {},
                "gen_triggers": gen_triggers or [],
                "dedup": True if dedup is None else bool(dedup),
                "chunk": True if chunk is None else bool(chunk),
            }
            with self._manager.write() as conn:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES ('policy', ?)",
                    (json.dumps(stored),),
                )
            return stored

        stored = json.loads(row["value"])
        if rules and rules != stored["rules"]:
            warnings.warn(
                "Supplied rules differ from the stored configuration and "
                "have been ignored. Rules cannot be changed after a store "
                "is created.",
                UserWarning,
                stacklevel=3,
            )
        if gen_triggers and gen_triggers != stored["gen_triggers"]:
            warnings.warn(
                "Supplied gen_triggers differ from the stored configuration "
                "and have been ignored. gen_triggers cannot be changed "
                "after a store is created.",
                UserWarning,
                stacklevel=3,
            )
        for name, supplied in (("dedup", dedup), ("chunk", chunk)):
            if supplied is not None and bool(supplied) != stored[name]:
                warnings.warn(
                    f"Supplied {name}={supplied!r} differs from the stored "
                    f"policy ({name}={stored[name]!r}) and has been "
                    "ignored. dedup/chunk are persisted store policy, set "
                    "once at creation.",
                    UserWarning,
                    stacklevel=3,
                )
        return stored

    # ------------------------------------------------------------------
    # Node creation
    # ------------------------------------------------------------------

    @contextmanager
    def create_node(
        self, step_type: str, parent: ParentArg = None
    ) -> Iterator[RecordingNode]:
        """Creates a new node while enforcing lineage rules.

        Yields a recording handle: write artifacts with ``node / "file"``
        and attach metadata with ``node.add_meta``. On clean exit the node
        is committed atomically; if the block raises, whatever was written
        is persisted with ``healthy=False`` (partial work is evidence). An
        untouched node is discarded with a warning.

        Args:
            step_type: The type of pipeline step being performed.
            parent: A Node/handle/node_id, or a list of them for a join.
                Every parent must exist in this store. None creates a root.

        Raises:
            InvalidTransition: If the transition is not permitted.
            ValueError: For an unusable step_type or unknown parent.
        """
        validate_step_type(step_type)
        parents = self._resolve_parents(parent)
        self._engine.validate(step_type, [p.step_type for p in parents])
        generation = self._engine.generation_for(
            step_type, [p.generation for p in parents]
        )

        node_id = uuid.uuid4().hex[:8]
        while self._metadata.exists(node_id) or (
            self.root / ".scratch" / node_id
        ).exists():
            node_id = uuid.uuid4().hex[:8]

        parent_ids = [p.node_id for p in parents]
        workspace = NodeWorkspace(
            self.root, node_id, step_type, parent_ids, generation=generation
        )
        handle = RecordingNode(node_id, step_type, generation, parent_ids, workspace)

        start = time.monotonic()
        try:
            yield handle
        except BaseException:
            # Keep partial work: anything recorded before the failure
            # persists, flagged unhealthy. An untouched node leaves no trace.
            self._finalize(
                handle, workspace, healthy=False, duration=time.monotonic() - start
            )
            raise

        if not self._finalize(
            handle, workspace, healthy=True, duration=time.monotonic() - start
        ):
            warnings.warn(
                f"Node '{handle.node_id}' (step_type='{step_type}') was "
                "discarded: no artifacts were written and no metadata was "
                "added. Write at least one file or call node.add_meta() to "
                "persist the node.",
                UserWarning,
                # 1=here, 2=contextlib.__exit__, 3=user `with` statement.
                stacklevel=3,
            )

    def _resolve_parents(self, parent: ParentArg) -> List[NodeRecord]:
        """Resolves a create_node parent argument — None, one node-like, or
        a list (a join) — to records from THIS store, de-duplicated and
        order-preserving. Raises ValueError for a parent the store cannot
        resolve, so a child can never hang off a phantom."""
        if parent is None:
            return []
        items: List[NodeLike]
        if isinstance(parent, (list, tuple)):
            items = list(parent)
        else:
            items = [parent]
        records: List[NodeRecord] = []
        seen = set()
        for item in items:
            if item is None:
                continue
            pid = (
                item.node_id
                if isinstance(item, (Node, RecordingNode))
                else str(item)
            )
            if pid in seen:
                continue
            record = self._metadata.get(pid)
            if record is None:
                raise ValueError(
                    f"Parent node {pid!r} is not present in this store. A "
                    "parent must be a node already created in or loaded "
                    "from this store; pass parent=None to create a root "
                    "node."
                )
            seen.add(pid)
            records.append(record)
        return records

    def _finalize(
        self,
        handle: RecordingNode,
        workspace: NodeWorkspace,
        healthy: bool,
        duration: float,
    ) -> bool:
        """Ingests the node if the block recorded anything (files or
        metadata); discards the workspace and returns False otherwise.

        Clean completions are fingerprinted: with dedup on, a node
        content-identical to an existing one is not persisted again — the
        handle is rebound onto the existing node instead. Failed runs are
        never deduplicated (partial work must not merge into a healthy
        node) and carry no content_hash."""
        files = workspace.files()
        if not files and not handle._entries:
            workspace.discard()
            return False

        content_hash: Optional[str] = None
        if healthy:
            envelopes = {
                entry.key: {
                    "value": entry.value,
                    "data_type": entry.data_type,
                    "group": entry.group,
                    "searchable": entry.searchable,
                }
                for entry in handle._entries.values()
            }
            digests = {
                relpath: hashlib.sha256(path.read_bytes()).hexdigest()
                for relpath, path in files
            }
            summary = ContentSummary.of(
                handle.step_type, handle.parent_id, envelopes, digests
            )
            content_hash = summary.digest
            if self.dedup and self._adopt_if_duplicate(handle, workspace, summary):
                return True

        prov = capture()
        record = NodeRecord(
            node_id=handle.node_id,
            step_type=handle.step_type,
            generation=handle.generation,
            created_utc=datetime.now(timezone.utc).isoformat(),
            created_epoch=time.time(),
            healthy=healthy,
            duration_s=round(duration, 3),
            size_bytes=0,  # recomputed by ingest from the actual files
            content_hash=content_hash,
            prov_user=prov["user"],
            prov_python=prov["python_version"],
            prov_platform=prov["platform"],
            prov_git_commit=prov["git_commit"],
            prov_git_branch=prov["git_branch"],
            prov_git_dirty=prov["git_dirty"],
            parent_ids=tuple(handle.parent_id),
        )
        rows = [
            metadata_row(
                entry.key,
                entry.value,
                data_type=entry.data_type,
                group=entry.group,
                searchable=entry.searchable,
            )
            for entry in handle._entries.values()
        ]
        ingest_node(
            self._manager, self._metadata, self._chunks, workspace, record, rows
        )
        return True

    def _adopt_if_duplicate(
        self,
        handle: RecordingNode,
        workspace: NodeWorkspace,
        summary: ContentSummary,
    ) -> bool:
        """If a persisted node is content-identical to what the block just
        produced, rebinds the handle onto it in place (the user's
        ``with ... as node`` variable transparently becomes the existing
        node, including as a later parent) and discards the scratch. The
        fingerprint only nominates a candidate; full ContentSummary
        equality decides, so a collision can never merge distinct nodes."""
        candidate_id = self._metadata.find_by_hash(summary.digest)
        if candidate_id is None:
            return False
        record = self._metadata.get(candidate_id)
        if record is None:
            return False
        existing = ContentSummary.of(
            record.step_type,
            record.parent_ids,
            self._metadata_envelopes(candidate_id),
            {
                relpath: artifact.sha256
                for relpath, artifact in self._chunks.artifact_manifest(
                    candidate_id
                ).items()
            },
        )
        if existing != summary:
            return False  # a hash collision, not a duplicate: keep both
        handle.node_id = record.node_id
        handle.step_type = record.step_type
        handle.generation = record.generation
        handle.parent_id = list(record.parent_ids)
        workspace.discard()
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _node_id_of(self, node: NodeLike) -> Optional[str]:
        if node is None:
            return None
        if isinstance(node, (Node, RecordingNode)):
            return node.node_id
        text = str(node)
        return None if text.lower() == "none" else text

    def _to_node(self, record: NodeRecord) -> Node:
        return Node(
            node_id=record.node_id,
            step_type=record.step_type,
            generation=record.generation,
            parent_id=record.parent_ids,
            created_utc=record.created_utc,
            healthy=record.healthy,
            duration_s=record.duration_s,
            size_bytes=record.size_bytes,
            content_hash=record.content_hash,
            _provenance={
                "user": record.prov_user,
                "python_version": record.prov_python,
                "platform": record.prov_platform,
                "git_commit": record.prov_git_commit,
                "git_dirty": record.prov_git_dirty,
                "git_branch": record.prov_git_branch,
            },
            _store=self,
        )

    def _nodes_for_ids(self, node_ids: Sequence[str]) -> List[Node]:
        nodes: List[Node] = []
        for node_id in node_ids:
            record = self._metadata.get(node_id)
            if record is not None:
                nodes.append(self._to_node(record))
        return nodes

    def get(self, node: NodeLike) -> Optional[Node]:
        """Resolves a node_id (or an existing Node/handle) into a Node
        record. Returns None for None, "none", or an unknown id."""
        if isinstance(node, Node):
            return node
        node_id = self._node_id_of(node)
        if node_id is None:
            return None
        record = self._metadata.get(node_id)
        return None if record is None else self._to_node(record)

    def find(self, **filters: Any) -> List[Node]:
        """Nodes matching every filter, oldest first. Filters match
        structural attributes (step_type, generation, healthy, ...) and
        searchable metadata by equality; pass a callable for a predicate
        (it receives the stored value, or None when the key is absent).

        Examples:
            >>> store.find(step_type="model")
            >>> store.find(accuracy=lambda a: a is not None and a > 0.9)
        """
        return self._nodes_for_ids(self._metadata.find(**filters))

    def latest(self, **filters: Any) -> Optional[Node]:
        """The most recently created node matching the filters, or None."""
        node_id = self._metadata.most_recent(self._metadata.find(**filters))
        return None if node_id is None else self.get(node_id)

    def lineage(self, node: NodeLike) -> List[Node]:
        """The node's full ancestry plus itself, oldest first; every node
        appears once, after all of its parents.

        Raises:
            NodeNotFound: If the node is not in this store.
        """
        node_id = self._node_id_of(node)
        if node_id is None:
            return []
        return self._nodes_for_ids(self._metadata.lineage(node_id))

    def children(self, node: NodeLike) -> List[Node]:
        """The direct children of the node (empty for unknown ids)."""
        node_id = self._node_id_of(node)
        if node_id is None or not self._metadata.exists(node_id):
            return []
        return self._nodes_for_ids(self._metadata.children(node_id))

    def ancestors(self, node: NodeLike, **filters: Any) -> List[Node]:
        """``find`` restricted to the node's lineage: ancestors (plus the
        node itself) matching every filter, in lineage order."""
        node_id = self._node_id_of(node)
        if node_id is None:
            return []
        matches = set(self._metadata.find(**filters))
        return self._nodes_for_ids(
            [nid for nid in self._metadata.lineage(node_id) if nid in matches]
        )

    def from_parent(self, node: NodeLike, filename: str) -> List[Path]:
        """Shortcut for the previous step's outputs: matching artifact
        paths from the node's parent(s), in parent order. Empty when the
        node or its parents cannot be resolved."""
        if isinstance(node, (Node, RecordingNode)):
            parent_ids: Sequence[str] = list(node.parent_id)
        else:
            record = (
                self._metadata.get(self._node_id_of(node) or "")
                if node is not None
                else None
            )
            if record is None:
                return []
            parent_ids = record.parent_ids
        paths: List[Path] = []
        for parent_id in parent_ids:
            if self._metadata.exists(parent_id):
                paths.extend(self._artifact_paths(parent_id, filename))
        return paths

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(
        self, node: NodeLike, dry_run: bool = True, compact: bool = True
    ) -> List[Node]:
        """Deletes a node and the descendants it solely supports, and
        reclaims the space they occupied.

        A descendant is removed only when EVERY one of its parents is also
        being removed — a child still reachable from an unpruned branch
        survives, and its edge to the pruned parent disappears via the
        foreign-key cascade. Preview with the default ``dry_run=True``,
        which never deletes and never compacts.

        Args:
            node: The node to prune.
            dry_run: Preview only (the default). Nothing is deleted.
            compact: Reclaim the freed chunk space afterwards (the
                default). Pass False when pruning many nodes in a loop and
                call ``compact()`` once at the end — compaction scans the
                whole chunk pool, so doing it per node is wasted work.

        Returns:
            The nodes that were (or would be) deleted, deepest first.
        """
        node_id = self._node_id_of(node)
        if node_id is None:
            return []
        doomed = self._pruner.plan(node_id)
        nodes = self._nodes_for_ids(doomed)  # fetched before deletion
        if not dry_run and doomed:
            self._pruner.delete(doomed)
            if compact:
                self.compact()
        return nodes

    def compact(self) -> int:
        """Reclaims space: deletes chunks no artifact references (a delta
        base still in use survives — the one-hop closure of AD5), then
        returns freed pages to the OS via ``incremental_vacuum`` and
        truncates the WAL.

        ``prune`` calls this for you, so it is only needed directly after a
        batch of ``prune(..., compact=False)`` calls, or to tidy a store
        pruned by an older version. It does not touch the session read
        cache (``<root>/.cache/``), which is transient and cleaned up
        automatically when the store closes.

        Returns:
            The number of chunks removed.
        """
        return compact_chunks(self._manager)

    def export(self, dest: Optional[Union[Path, str]] = None) -> Path:
        """Writes grep-able JSON sidecars: one ``meta.json`` per node under
        ``<dest>/<node_id>/`` (default ``<root>/export``), holding the
        node's structural facts, provenance, metadata envelopes and
        artifact digests. The database remains the source of truth — this
        is the file-portability escape hatch (AD9), so lineage stays
        legible to grep even if ancestree is uninstalled tomorrow.

        Returns:
            The export directory.
        """
        dest_dir = Path(dest) if dest is not None else self.root / "export"
        for node_id in self._metadata.all_node_ids():
            record = self._metadata.get(node_id)
            if record is None:
                continue
            document = {
                "node_id": record.node_id,
                "step_type": record.step_type,
                "generation": record.generation,
                "parent_id": list(record.parent_ids),
                "created_utc": record.created_utc,
                "healthy": record.healthy,
                "duration_s": record.duration_s,
                "size_bytes": record.size_bytes,
                "content_hash": record.content_hash,
                "provenance": self._to_node(record).provenance,
                "metadata": self._metadata_envelopes(node_id),
                "artifacts": {
                    relpath: {"size": artifact.size, "sha256": artifact.sha256}
                    for relpath, artifact in sorted(
                        self._chunks.artifact_manifest(node_id).items()
                    )
                },
            }
            out = dest_dir / node_id / "meta.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(document, indent=2))
        return dest_dir

    def backup(self, dest: Union[Path, str]) -> Path:
        """Writes a consistent, self-contained copy of the whole store —
        safe to take while the store is open and being written to.

        A *live* store is not one file: WAL journalling keeps recent commits
        in ``ancestree.db-wal`` until a checkpoint, so copying
        ``ancestree.db`` out from under an open store silently loses
        everything since the last one. This goes through SQLite's online
        backup API, which reads through the WAL and produces a fully
        checkpointed single file — the correct way to back a store up
        without closing it.

        Args:
            dest: A path ending in ``.db``, written as that file; any other
                path is treated as a store root and given an
                ``ancestree.db`` inside it, so ``LineageStore(dest)`` opens
                the copy directly. An existing destination is overwritten.

        Returns:
            The path of the database file written.

        Raises:
            ValueError: If the destination is this store's own database.
        """
        dest_path = Path(dest)
        target = dest_path if dest_path.suffix == ".db" else dest_path / DB_FILENAME
        if target.resolve() == self._manager.db_path.resolve():
            raise ValueError(
                "Refusing to back a store up onto itself; choose a different "
                "destination."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            self._manager.read().backup(destination)
        finally:
            destination.close()
        return target

    # ------------------------------------------------------------------
    # Visualisation
    # ------------------------------------------------------------------

    def generate_web_graph(
        self,
        dest: Optional[Union[Path, str]] = None,
        include_artifacts: bool = True,
    ) -> Path:
        """Renders the store into one shareable HTML file — a **view-only
        snapshot**: the lineage graph plus click-to-view metadata (search
        lives in the live server, so the query grammar exists once).

        Small images inline as data URIs; other artifacts are copied
        beside the file (``<name>_files/``) so links work offline. Pass
        ``include_artifacts=False`` for a metadata-only snapshot of a
        store with huge artifacts. Defaults to
        ``<root>/interactive_pipeline.html``; returns the written path.
        """
        return export_static(self, dest=dest, include_artifacts=include_artifacts)

    def host_live_graph(self, port: int = 0, block: bool = False, open_browser: bool = True) -> str:
        """Serves the searchable explorer on ``127.0.0.1`` and returns its URL.
        
        By default, this method runs non-blocking (``block=False``) and automatically
        opens the live application graph in your default web browser (``open_browser=True``).
        Calling it again replaces the running background server — re-running a
        notebook cell restarts the explorer rather than leaking a second one.

        Args:
            port: Port to bind to. Defaults to 0 (assigns an ephemeral, free port).
            block: If True, blocks execution using a loop until a KeyboardInterrupt is received.
            open_browser: If True, automatically launches the application in the system browser.

        Returns:
            str: The local URL running the application server.
        """
        import webbrowser
        from .web.server import start_server

        # Re-running the call (a notebook cell) replaces the previous
        # background server instead of leaking it until close().
        previous = self._resources.get("live_server")
        if previous is not None:
            self._resources["live_server"] = None
            previous.close()

        handle = start_server(self, port=port)
        url = handle.url
        print(f"Ancestree explorer running at: {url}  (Store close will terminate background thread)")
        
        # Automatically launch browser window/tab if requested
        if open_browser:
            # A short delay can sometimes be useful if your local backend 
            # requires split-second initialization, though start_server handles it.
            webbrowser.open(url)

        if not block:
            self._resources["live_server"] = handle  # Automatically closed when store.close() runs
            return url
            
        try:
            while True:
                handle._thread.join(1)
        except KeyboardInterrupt:
            print("\nStopping Ancestree explorer server...")
        finally:
            handle.close()
            
        return url

    # ------------------------------------------------------------------
    # Power tools
    # ------------------------------------------------------------------

    def sql(self, query: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        """Runs a read-only SELECT over the documented schema (blueprint
        section 6) and returns the rows. The connection is opened read-only
        with ``PRAGMA query_only``, so writes are impossible by
        construction — the escape hatch can never corrupt invariants.

        Examples:
            >>> store.sql("SELECT step_type, count(*) FROM node GROUP BY 1")
        """
        conn = sqlite3.connect(
            f"file:{self._manager.db_path}?mode=ro", uri=True
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return conn.execute(query, tuple(params)).fetchall()
        finally:
            conn.close()

    def stats(self) -> Dict[str, Any]:
        """Store-level numbers that make deduplication visible: node/
        artifact/chunk counts, logical vs stored bytes, the dedup ratio
        (logical ÷ stored; higher is better) and the store's size on disk.

        ``database_bytes`` counts the database file **plus its write-ahead
        log**: mid-session most recently written bytes live in the WAL, so
        counting only ``ancestree.db`` understates real disk usage by
        orders of magnitude until the next checkpoint.
        """
        conn = self._manager.read()
        nodes = conn.execute("SELECT count(*) AS n FROM node").fetchone()["n"]
        art = conn.execute(
            "SELECT count(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM artifact"
        ).fetchone()
        chunks = conn.execute(
            "SELECT count(*) AS n, COALESCE(SUM(length), 0) AS plain, "
            "COALESCE(SUM(LENGTH(data)), 0) AS stored FROM chunk"
        ).fetchone()
        stored = int(chunks["stored"])
        logical = int(art["bytes"])
        return {
            "nodes": int(nodes),
            "artifacts": int(art["n"]),
            "chunks": int(chunks["n"]),
            "logical_bytes": logical,
            "chunk_plain_bytes": int(chunks["plain"]),
            "chunk_stored_bytes": stored,
            "database_bytes": self._database_bytes(),
            "dedup_ratio": round(logical / stored, 3) if stored else None,
        }

    def _database_bytes(self) -> int:
        """The store's real size on disk: the database file plus its WAL."""
        db_path = self._manager.db_path
        total = db_path.stat().st_size
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.exists():
            total += wal.stat().st_size
        return total

    # ------------------------------------------------------------------
    # Internals the Node record reads through
    # ------------------------------------------------------------------

    def _metadata_envelopes(self, node_id: str) -> Dict[str, Dict[str, Any]]:
        return {
            key: {
                "value": row.value,
                "data_type": row.data_type,
                "group": row.group,
                "searchable": row.searchable,
            }
            for key, row in self._metadata.metadata_for(node_id).items()
        }

    def _artifact_paths(self, node_id: str, contains: str = "*") -> List[Path]:
        relpaths = sorted(self._chunks.artifact_manifest(node_id))
        return [
            self._chunks.reassemble(node_id, relpath)
            for relpath in filter_relpaths(relpaths, contains)
        ]

    def _artifact_path(self, node_id: str, relpath: str) -> Path:
        return self._chunks.reassemble(node_id, relpath)
