"""Destructive and space operations: prune, compact, orphan-scratch sweep.

Top-level because these cut across the layers: the Pruner plans over the
metadata DAG and deletes rows (edges, metadata and artifact recipes cascade
via foreign keys); ``compact_chunks`` removes chunks nothing references any
more and returns freed pages to the OS; ``sweep_orphan_scratch`` runs at
store open and adopts a dead process's seeded scratch as an unhealthy node
— partial work stays evidence even across a SIGKILL, which 0.1.x lost —
and reaps read-cache sessions (``<root>/.cache/``) whose owner is gone.

No lock files and no grace windows: synchronous ingest commits a node's
chunks and recipes atomically, so compact can never observe a chunk whose
recipe is still in flight — a concurrent writer simply holds the database
write lock until its transaction lands.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 5, issue #16).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from .db.chunk_store import ChunkStore
from .db.connection import ConnectionManager
from .db.metadata_store import MetadataStore, NodeRecord
from .ingest.packing import ingest_node
from .ingest.workspace import SEED_FILENAME, STAGING_PREFIX, NodeWorkspace

#: How long an unseeded staging directory is left alone before being treated
#: as litter. It only has to cover the microseconds between a staging
#: directory being created and its seed being written.
_STAGING_GRACE_SECONDS = 300


class Pruner:
    """DAG-aware deletion: a descendant dies only when every one of its
    parents is also being deleted. A child still reachable from an unpruned
    branch survives — and its edge to the pruned parent disappears via the
    foreign-key cascade, with no bookkeeping (0.1.x had to rewrite the
    survivors' parent lists by hand)."""

    def __init__(self, manager: ConnectionManager, metadata: MetadataStore) -> None:
        self._manager = manager
        self._metadata = metadata

    def plan(self, node_id: str) -> list[str]:
        """The ids pruning `node_id` would delete, deepest first (children
        before parents). Empty for an unknown id; nothing is deleted."""
        if not self._metadata.exists(node_id):
            return []

        # 1. The target and all transitive descendants — the region a
        #    deletion could touch.
        affected: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in affected:
                continue
            affected.add(current)
            stack.extend(self._metadata.children(current))

        all_parents: dict[str, list[str]] = {}
        children_in_region: dict[str, list[str]] = {nid: [] for nid in affected}
        for nid in affected:
            record = self._metadata.get(nid)
            parents = list(record.parent_id) if record else []
            all_parents[nid] = parents
            for parent in parents:
                if parent in affected:
                    children_in_region[parent].append(nid)

        # 2. Topological order (a node after its in-region parents) via
        #    Kahn, then decide in that order: the target goes, and a
        #    descendant goes only if ALL of its parents (anywhere) are
        #    going. A surviving parent spares the child.
        indegree = {
            nid: sum(1 for p in all_parents[nid] if p in affected) for nid in affected
        }
        ready = [nid for nid, degree in indegree.items() if degree == 0]
        topo: list[str] = []
        while ready:
            nid = ready.pop()
            topo.append(nid)
            for child in children_in_region[nid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)

        doomed: set[str] = set()
        for nid in topo:
            if nid == node_id or all(p in doomed for p in all_parents[nid]):
                doomed.add(nid)

        return [nid for nid in reversed(topo) if nid in doomed]

    def delete(self, node_ids: Sequence[str]) -> None:
        """Deletes the rows in one transaction; edges, metadata and
        artifact recipes cascade. Chunk space is reclaimed separately by
        compact_chunks."""
        if not node_ids:
            return
        with self._manager.write() as conn:
            for start in range(0, len(node_ids), 500):
                batch = list(node_ids[start : start + 500])
                placeholders = ",".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM node WHERE node_id IN ({placeholders})",
                    batch,
                )


def compact_chunks(manager: ConnectionManager) -> int:
    """Deletes every chunk nothing references and returns how many went.

    A chunk is live if any artifact recipe uses it, or if it is the delta
    base of a live chunk — a single-hop closure, since delta depth is
    capped at 1 (AD5). Afterwards ``incremental_vacuum`` hands the freed
    pages back to the OS and the WAL is checkpointed, so the file actually
    shrinks."""
    with manager.write() as conn:
        cursor = conn.execute(
            "DELETE FROM chunk WHERE digest NOT IN ("
            "  SELECT digest FROM artifact_chunk"
            "  UNION"
            "  SELECT base_digest FROM chunk"
            "  WHERE base_digest IS NOT NULL"
            "    AND digest IN (SELECT digest FROM artifact_chunk)"
            ")"
        )
        removed = int(cursor.rowcount)
    conn = manager.read()
    # The WAL has to be folded in first: pages freed by the DELETE are only
    # recorded there until a checkpoint, and incremental_vacuum can only
    # truncate the main database file.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    # incremental_vacuum is a STEPPED statement — one page per step — so the
    # cursor must be driven to exhaustion. Executing it without consuming the
    # result frees exactly one page per call.
    conn.execute("PRAGMA incremental_vacuum").fetchall()
    manager.checkpoint()
    return removed


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe. POSIX signal-0 semantics; on platforms
    where that is unreliable a false negative merely delays adoption to a
    later store open."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _reap_staging(entry: Path) -> None:
    """Removes an abandoned staging directory.

    A staging directory exists only between a node's scratch being created
    and it being renamed into place, which happens before any user code
    runs — so it never holds artifacts. It must still never be removed
    while that is in progress: a live owner, or a directory too young to
    have been seeded yet, is left alone.
    """
    seed_path = entry / SEED_FILENAME
    try:
        if seed_path.exists():
            seed = json.loads(seed_path.read_text())
            pid = seed.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                return  # still being renamed into place
        elif time.time() - entry.stat().st_mtime < _STAGING_GRACE_SECONDS:
            return  # created moments ago; its seed is on its way
    except (json.JSONDecodeError, OSError):
        pass
    shutil.rmtree(entry, ignore_errors=True)


def _sweep_stale_cache(root: Path) -> None:
    """Reaps read-cache sessions whose owning process is dead.

    Cache directories are pid-tagged (``<pid>-<suffix>``) and hold pure
    derived data — anything removed regenerates from the chunk pool on the
    next read — so removal can never lose work. Directories that are not
    session-shaped are litter and go too. Live sessions are never touched.
    """
    cache_root = root / ".cache"
    if not cache_root.exists():
        return
    for entry in cache_root.iterdir():
        if not entry.is_dir():
            continue
        try:
            pid = int(entry.name.split("-", 1)[0])
        except ValueError:
            shutil.rmtree(entry, ignore_errors=True)
            continue
        if not _pid_alive(pid):
            shutil.rmtree(entry, ignore_errors=True)


def sweep_orphan_scratch(
    root: str | Path,
    manager: ConnectionManager,
    metadata: MetadataStore,
    chunks: ChunkStore,
) -> list[str]:
    """Adopts scratch directories orphaned by a hard-killed session, and
    reaps dead sessions' read-cache directories.

    Runs at store open. For each ``.scratch/<id>/`` whose seed names a dead
    process: a directory holding artifacts and no committed row becomes an
    **unhealthy node** (the files are ingested as evidence); a directory
    whose node already committed is just deleted; unseeded, unreadable or
    empty directories are litter and are removed. Directories owned by a
    live process are never touched. Returns the adopted node ids."""
    _sweep_stale_cache(Path(root))
    scratch_root = Path(root) / ".scratch"
    if not scratch_root.exists():
        return []
    adopted: list[str] = []
    for entry in sorted(scratch_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(STAGING_PREFIX):
            # A node being set up right now, by this or another process.
            # Never a candidate for adoption, and only litter once its
            # owner is gone.
            _reap_staging(entry)
            continue
        seed_path = entry / SEED_FILENAME
        if not seed_path.exists():
            shutil.rmtree(entry, ignore_errors=True)  # unseeded litter
            continue
        try:
            seed = json.loads(seed_path.read_text())
        except (json.JSONDecodeError, OSError):
            shutil.rmtree(entry, ignore_errors=True)
            continue
        pid = seed.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            continue  # the owning block is still running; not ours

        node_id = str(seed.get("node_id") or entry.name)
        if metadata.exists(node_id):
            # Crashed between the commit and the scratch cleanup: the node
            # is durable, the directory is just leftovers.
            shutil.rmtree(entry, ignore_errors=True)
            continue

        workspace = NodeWorkspace(
            root,
            node_id,
            str(seed.get("step_type") or "unknown"),
            [str(p) for p in seed.get("parent_id") or []],
            generation=int(seed.get("generation") or 0),
        )
        if not workspace.files():
            workspace.discard()  # seeded but never written: no evidence
            continue
        try:
            started = str(seed.get("started_utc") or "")
            try:
                epoch = datetime.fromisoformat(started).timestamp()
            except ValueError:
                started = datetime.now(timezone.utc).isoformat()
                epoch = time.time()
            record = NodeRecord(
                node_id=node_id,
                step_type=str(seed.get("step_type") or "unknown"),
                generation=int(seed.get("generation") or 0),
                created_utc=started,
                created_epoch=epoch,
                healthy=False,  # a hard kill is never a clean completion
                parent_id=tuple(str(p) for p in seed.get("parent_id") or []),
            )
            ingest_node(manager, metadata, chunks, workspace, record)
        except Exception:  # noqa: BLE001, S112 - best effort; the evidence is kept either way
            # Adoption failed (e.g. a parent was pruned since): keep the
            # directory as-is — the evidence survives for a manual look or
            # a later open.
            continue
        adopted.append(node_id)
    return adopted
