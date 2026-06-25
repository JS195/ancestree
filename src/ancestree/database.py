# Python packages
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .utils import parse_iso_utc, is_match, flatten_meta

# A flat index entry: searchable metadata key -> value.
IndexEntry = Dict[str, Any]


class lineage_database:
    """In-memory index of node metadata, persisted as a disposable snapshot
    plus an append-only journal in the store root. meta.json files remain the
    source of truth: the index is reconciled against the directory listing on
    load and can be rebuilt from disk at any time.

    Writes do not rewrite the whole index. Each add/remove appends a single
    line to ``.index.log`` (O(1)), and the log is periodically compacted back
    into ``.index.json`` once it has grown past the snapshot size. This keeps
    node creation flat regardless of how many nodes already exist; the older
    design rewrote every entry on every add, which made creation O(N) per node
    and O(N**2) overall.

    Process-safe via atomic snapshot replacement, atomic log appends and
    mtime-based cache invalidation, with directory reconciliation as the
    backstop: a torn or lost journal line is recovered from the on-disk
    meta.json on the next load. Not thread-safe — concurrent access from
    multiple threads within the same process is not supported.
    """

    # Floor below which compaction would thrash on tiny stores. Above it the
    # threshold tracks the snapshot size, so compactions happen on a doubling
    # schedule and the amortised cost of an append stays O(1).
    _COMPACT_MIN = 128

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)
        self.snapshot_path = self.root / ".index.json"
        self.log_path = self.root / ".index.log"
        self._cache: Optional[Dict[str, IndexEntry]] = None
        self._snapshot_mtime: Optional[int] = None
        self._log_mtime: Optional[int] = None
        self._since_compact = 0
        self._compacted_size = 0
        # Reverse lookup content_hash -> node_id for deduplication. Derived
        # from the cache and rebuilt lazily; kept in sync on add/remove so
        # dedup stays O(1) per node rather than scanning the whole index.
        self._hash_index: Optional[Dict[str, str]] = None

    @staticmethod
    def _mtime_ns(path: Path) -> Optional[int]:
        # No exists() guard: the journal can be unlinked by a concurrent
        # compaction between the check and the stat, so swallow the race.
        try:
            return path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _is_stale(self) -> bool:
        if self._cache is None:
            return True
        return (
            self._mtime_ns(self.snapshot_path) != self._snapshot_mtime
            or self._mtime_ns(self.log_path) != self._log_mtime
        )

    @property
    def cache(self) -> Dict[str, IndexEntry]:
        if self._cache is None:
            self._load()
        assert self._cache is not None
        return self._cache

    def _load(self) -> None:
        if not self.snapshot_path.exists():
            self.rebuild_from_disk()
            return
        snap_mtime = self.snapshot_path.stat().st_mtime_ns  # stat before read
        try:
            self._cache = json.loads(self.snapshot_path.read_text())
        except json.JSONDecodeError:
            raise RuntimeError(
                "The index snapshot (.index.json) is corrupt. "
                "Call store.rebuild_db_from_disk() to recover."
            ) from None
        assert self._cache is not None
        self._snapshot_mtime = snap_mtime
        self._compacted_size = len(self._cache)
        self._hash_index = None  # cache replaced; rebuild lazily on next lookup
        self._replay_log()
        self._reconcile()

    def _replay_log(self) -> None:
        """Applies the append-only journal on top of the loaded snapshot."""
        assert self._cache is not None
        try:
            self._log_mtime = self.log_path.stat().st_mtime_ns  # stat before read
            raw = self.log_path.read_text()
        except FileNotFoundError:
            # No journal, or a concurrent compaction removed it mid-read. The
            # snapshot already holds the compacted state; reconcile fills any gap.
            self._log_mtime = None
            self._since_compact = 0
            return
        applied = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line from a concurrent append: skip it. The node
                # is recovered from disk by _reconcile if its meta.json exists.
                continue
            applied += 1
            if record.get("_op") == "del":
                self._cache.pop(record["id"], None)
            else:
                self._cache[record["id"]] = record["meta"]
        self._since_compact = applied

    def _reconcile(self) -> None:
        assert self._cache is not None
        on_disk = {d.name for d in self.root.iterdir() if (d / "meta.json").exists()}
        if on_disk != set(self._cache):
            for node_id in set(self._cache) - on_disk:
                del self._cache[node_id]
            for node_id in on_disk - set(self._cache):
                self._cache[node_id] = flatten_meta(
                    json.loads((self.root / node_id / "meta.json").read_text())
                )
            self._write_snapshot()

    def _append_log(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        # O_APPEND keeps concurrent writers from clobbering each other's lines.
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._log_mtime = self._mtime_ns(self.log_path)
        self._since_compact += 1
        if self._since_compact >= max(self._COMPACT_MIN, self._compacted_size):
            self._write_snapshot()

    def _write_snapshot(self) -> None:
        """Compacts the journal into the snapshot: writes the full cache out
        atomically and clears the log."""
        assert self._cache is not None
        # The temp name must be unique per writer: a shared name lets one
        # concurrent flush replace another's temp file out from under it.
        tmp = self.root / f".index.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_text(json.dumps(self._cache))
            tmp.replace(self.snapshot_path)
        finally:
            tmp.unlink(missing_ok=True)
        # Snapshot is now authoritative; dropping the log afterwards is safe
        # (a crash in between just replays already-snapshotted lines, which is
        # idempotent).
        self.log_path.unlink(missing_ok=True)
        self._snapshot_mtime = self.snapshot_path.stat().st_mtime_ns
        self._log_mtime = None
        self._since_compact = 0
        self._compacted_size = len(self._cache)

    def rebuild_from_disk(self) -> None:
        self._cache = {
            p.parent.name: flatten_meta(json.loads(p.read_text()))
            for p in self.root.glob("*/meta.json")
        }
        self._hash_index = None  # cache replaced; rebuild lazily on next lookup
        self._write_snapshot()

    def _refresh_if_stale(self) -> None:
        if self._is_stale():
            self._load()

    def add(self, node_id: str, meta: IndexEntry) -> None:
        self._refresh_if_stale()
        self.cache[node_id] = meta
        if self._hash_index is not None:
            content_hash = meta.get("content_hash")
            if content_hash:
                self._hash_index[content_hash] = node_id
        self._append_log({"id": node_id, "meta": meta})

    def remove(self, node_id: str) -> None:
        self._refresh_if_stale()
        entry = self.cache.pop(node_id, None)
        if self._hash_index is not None and entry:
            content_hash = entry.get("content_hash")
            # Only drop the mapping if it still points at this node; a later
            # duplicate could have overwritten it.
            if content_hash and self._hash_index.get(content_hash) == node_id:
                del self._hash_index[content_hash]
        self._append_log({"_op": "del", "id": node_id})

    def find_by_hash(self, content_hash: str) -> Optional[str]:
        """Returns the node_id of an indexed node whose content_hash matches,
        or None. Backs deduplication: the store treats the result as a
        candidate and byte-verifies it before reuse."""
        self._refresh_if_stale()
        if self._hash_index is None:
            index: Dict[str, str] = {}
            for nid, meta in self.cache.items():
                stored = meta.get("content_hash")
                if stored:
                    index[stored] = nid
            self._hash_index = index
        return self._hash_index.get(content_hash)

    def find_matches(self, **kwargs: Any) -> List[str]:
        self._refresh_if_stale()
        return [k for k, m in self.cache.items() if is_match(m, **kwargs)]

    def find_in_lineage(self, curr_node: str, **kwargs: Any) -> List[str]:
        return [
            k for k in self.get_lineage(curr_node) if is_match(self.cache[k], **kwargs)
        ]

    @staticmethod
    def _parents(entry: IndexEntry) -> List[str]:
        """The parent ids of an index entry (a list; empty for a root)."""
        return entry.get("parent_id") or []

    def find_children(self, node_id: str) -> List[str]:
        """Returns the ids of nodes that list `node_id` among their parents."""
        self._refresh_if_stale()
        return [
            nid for nid, entry in self.cache.items() if node_id in self._parents(entry)
        ]

    def get_lineage(self, curr_node: Optional[str]) -> List[str]:
        """Returns every ancestor of `curr_node` plus `curr_node` itself, in
        topological order (oldest first), following all parents. For a linear
        chain this is the chain; for a DAG (a join) it is the union of all the
        inputs' histories, each node listed once and after all of its parents."""
        self._refresh_if_stale()
        if not curr_node:
            return []
        order: List[str] = []
        done: set[str] = set()  # fully emitted
        on_stack: set[str] = set()  # ancestors currently being walked (cycle guard)

        # Iterative post-order DFS over parents: a node is emitted only after all
        # of its parents, so the result is oldest-first. Done iteratively so deep
        # lineages cannot exhaust the recursion limit.
        stack: List[Tuple[str, bool]] = [(curr_node, False)]
        while stack:
            nid, expanded = stack.pop()
            if nid in done:
                continue
            if nid not in self.cache:
                raise KeyError(
                    f"Node '{nid}' not found in the index. "
                    "Call store.rebuild_db_from_disk() to resync the index."
                )
            if expanded:
                on_stack.discard(nid)
                done.add(nid)
                order.append(nid)
                continue
            if nid in on_stack:
                raise ValueError(
                    f"Cycle detected in lineage at node '{nid}'. "
                    "The store metadata may be corrupted."
                )
            on_stack.add(nid)
            stack.append((nid, True))  # emit after its parents
            for pid in self._parents(self.cache[nid]):
                if pid not in done:
                    stack.append((pid, False))
        return order

    def most_recent(self, node_ids: List[str]) -> Optional[str]:
        # Takes an already-matched id list rather than calling find_matches
        # itself: that keeps the caller (get_most_recent_node) at the same call
        # depth from is_match as find_node, so a raising predicate's warning
        # points at user code with the same stacklevel.
        return max(
            node_ids,
            default=None,
            key=lambda k: parse_iso_utc(self.cache[k]["timestamp"]),
        )
