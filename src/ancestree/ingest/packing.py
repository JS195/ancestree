"""The ingest pipeline: scratch files -> chunks -> SQLite, in one transaction.

``ingest_node`` is the write→durable transform: it reads a node's scratch
files once, chunks them (Layer 1 CDC, or the fixed-boundary fallback for
huge files), deduplicates into the chunk pool, and commits the node row,
edges, metadata, chunks and artifact recipes as ONE transaction — the node
appears to every reader wholly or not at all, and a failure anywhere leaves
the database untouched and the scratch intact. Runs synchronously at block
exit (AD4); artifact SHA-256s and the node's total size are computed in the
same pass and returned for node-level dedup (Phase 5).

The MetadataStore and ChunkStore must share this manager — nested
``write()`` blocks join one transaction only within a single
ConnectionManager.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 3, issue #14).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Dict, Sequence

from ..db.chunk_store import ChunkStore
from ..db.connection import ConnectionManager
from ..db.metadata_store import MetadataRow, MetadataStore, NodeRecord
from .cdc import LARGE_FILE_THRESHOLD, chunk_bytes
from .workspace import NodeWorkspace

# Past this much new artifact data, fold the WAL back into the database so
# the sidecar never balloons (see ConnectionManager.checkpoint).
_CHECKPOINT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class IngestResult:
    """What an ingest produced: the node record as persisted (with its
    final size) and each artifact's SHA-256 — the inputs node-level
    deduplication needs (Phase 5)."""

    record: NodeRecord
    artifact_sha256s: Dict[str, str]


def ingest_node(
    manager: ConnectionManager,
    metadata_store: MetadataStore,
    chunk_store: ChunkStore,
    workspace: NodeWorkspace,
    record: NodeRecord,
    metadata: Sequence[MetadataRow] = (),
    large_file_threshold: int = LARGE_FILE_THRESHOLD,
) -> IngestResult:
    """Persists one node — row, edges, metadata, chunks, artifacts — in a
    single transaction, then deletes its scratch directory.

    The record's ``size_bytes`` is recomputed from the scratch files; pass
    it as 0. On any failure the transaction rolls back whole and the
    scratch directory is left in place, so nothing is lost.

    Returns:
        IngestResult: The persisted record and per-artifact SHA-256s.
    """
    files = workspace.files()
    total = sum(path.stat().st_size for _, path in files)
    final = replace(record, size_bytes=total)
    sha256s: Dict[str, str] = {}

    with manager.write() as conn:
        # Node row first: artifact rows hold a foreign key onto it.
        metadata_store.add_node(final, metadata)
        for relpath, path in files:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            sha256s[relpath] = digest
            chunk_digests = [
                chunk_store.put_chunk(conn, piece)
                for piece in chunk_bytes(
                    data, large_file_threshold=large_file_threshold
                )
            ]
            chunk_store.add_artifact(
                conn, final.node_id, relpath, len(data), digest, chunk_digests
            )

    if total >= _CHECKPOINT_BYTES:
        manager.checkpoint()
    workspace.discard()
    return IngestResult(record=final, artifact_sha256s=sha256s)
