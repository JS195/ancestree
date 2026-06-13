# Python packages
import json
import uuid
from pathlib import Path

from .utils import parse_iso_utc, is_match, flatten_meta


class lineage_database:
    """In-memory index of node metadata, persisted as a disposable JSON
    snapshot in the store root. meta.json files remain the source of truth:
    the snapshot is reconciled against the directory listing on load and can
    be rebuilt from disk at any time.

    Process-safe via atomic snapshot replacement and mtime-based cache
    invalidation. Not thread-safe — concurrent access from multiple threads
    within the same process is not supported.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.snapshot_path = self.root / ".index.json"
        self._cache = None
        self._loaded_at = None

    def _is_stale(self):
        if not self.snapshot_path.exists():
            return True
        if self._loaded_at is None:
            return True
        return self.snapshot_path.stat().st_mtime_ns != self._loaded_at

    @property
    def cache(self):
        if self._cache is None:
            self._load()
        return self._cache

    def _load(self):
        if self.snapshot_path.exists():
            self._loaded_at = self.snapshot_path.stat().st_mtime_ns  # stat before read
            self._cache = json.loads(self.snapshot_path.read_text())
            self._reconcile()
        else:
            self.rebuild_from_disk()

    def _reconcile(self):
        on_disk = {d.name for d in self.root.iterdir() if (d / "meta.json").exists()}
        if on_disk != set(self._cache):
            for node_id in set(self._cache) - on_disk:
                del self._cache[node_id]
            for node_id in on_disk - set(self._cache):
                self._cache[node_id] = flatten_meta(
                    json.loads((self.root / node_id / "meta.json").read_text())
                )
            self._flush()

    def _flush(self):
        # The temp name must be unique per writer: a shared name lets one
        # concurrent flush replace another's temp file out from under it.
        tmp = self.root / f".index.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_text(json.dumps(self.cache))
            tmp.replace(self.snapshot_path)
        finally:
            tmp.unlink(missing_ok=True)
        self._loaded_at = self.snapshot_path.stat().st_mtime_ns

    def rebuild_from_disk(self):
        self._cache = {
            p.parent.name: flatten_meta(json.loads(p.read_text()))
            for p in self.root.glob("*/meta.json")
        }
        self._flush()

    def _refresh_if_stale(self):
        if self._is_stale():
            self._load()

    def add(self, node_id, meta):
        self._refresh_if_stale()
        self.cache[node_id] = meta
        self._flush()

    def remove(self, node_id):
        self._refresh_if_stale()
        self.cache.pop(node_id, None)
        self._flush()

    def find_matches(self, **kwargs):
        self._refresh_if_stale()
        return [k for k, m in self.cache.items() if is_match(m, **kwargs)]

    def find_in_lineage(self, curr_node, **kwargs):
        return [
            k for k in self.get_lineage(curr_node) if is_match(self.cache[k], **kwargs)
        ]

    def get_lineage(self, curr_node):
        self._refresh_if_stale()
        history, visited = [], set()
        while curr_node:
            if curr_node in visited:
                raise ValueError(
                    f"Cycle detected in lineage at node '{curr_node}'. "
                    "The store metadata may be corrupted."
                )
            if curr_node not in self.cache:
                raise KeyError(
                    f"Node '{curr_node}' not found in the index. "
                    "Call store.rebuild_db_from_disk() to resync the index."
                )
            visited.add(curr_node)
            history.append(curr_node)
            curr_node = self.cache[curr_node].get("parent_id")
        return history[::-1]

    def get_most_recent(self, **kwargs):
        matches = self.find_matches(**kwargs)
        return max(
            matches,
            default=None,
            key=lambda k: parse_iso_utc(self.cache[k].get("timestamp")),
        )
