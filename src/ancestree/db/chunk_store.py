"""ChunkStore — chunk BLOBs, artifact recipes, reassembly and the read cache.

The content-addressed pool lives in the ``chunk`` table: each chunk is
stored once, keyed by the SHA-256 of its plaintext (``INSERT OR IGNORE`` is
the exact dedup). This phase stores raw zlib chunks (kind 0); delta chunks
(kind 1, zlib-zdict against a raw base) arrive in Phase 6.

Write methods take the caller's open write-transaction connection, so a
node's chunks, artifact rows and metadata commit as ONE atomic unit (AD3) —
the packing pipeline owns that transaction. Reads go through the manager's
thread-local connections.

Also owns the session **read cache**: reassembled artifacts land in a
``tempfile``-managed directory in the system temp dir — never under the
store root — so hard-killed leftovers are the OS temp-cleaner's problem and
the store at rest stays just the database.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 3, issue #14).
"""

from __future__ import annotations

import atexit
import hashlib
import shutil
import sqlite3
import tempfile
import time
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import ArtifactNotFound, CorruptChunkError, IntegrityError
from .connection import ConnectionManager

_KIND_RAW = 0
_KIND_DELTA = 1  # decode support arrives with Layer 2 (Phase 6)


@dataclass(frozen=True)
class ArtifactRecord:
    """One logical output file of a node: its identity plus the ordered
    chunk recipe that reconstructs it."""

    relpath: str
    size: int
    sha256: str
    chunk_digests: Tuple[str, ...]


class ChunkStore:
    """Reads and writes the chunk pool and artifact recipes of one store."""

    def __init__(self, manager: ConnectionManager) -> None:
        self._manager = manager
        self._cache_root: Optional[Path] = None

    # ------------------------------------------------------------------
    # Writes (the caller holds the write transaction)
    # ------------------------------------------------------------------

    def put_chunk(self, conn: sqlite3.Connection, data: bytes) -> str:
        """Stores one plaintext chunk (compressed) and returns its digest.

        A chunk already in the pool is left untouched — that INSERT OR
        IGNORE is exactly where exact deduplication happens.
        """
        digest = hashlib.sha256(data).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO chunk "
            "(digest, kind, base_digest, data, length, created_epoch) "
            "VALUES (?, ?, NULL, ?, ?, ?)",
            (digest, _KIND_RAW, zlib.compress(data), len(data), time.time()),
        )
        return digest

    def add_artifact(
        self,
        conn: sqlite3.Connection,
        node_id: str,
        relpath: str,
        size: int,
        sha256: str,
        chunk_digests: Sequence[str],
    ) -> None:
        """Records one artifact and its ordered chunk recipe. The chunks
        must already be in the pool (same transaction is fine)."""
        conn.execute(
            "INSERT INTO artifact (node_id, relpath, size, sha256) "
            "VALUES (?, ?, ?, ?)",
            (node_id, relpath, size, sha256),
        )
        conn.executemany(
            "INSERT INTO artifact_chunk (node_id, relpath, ordinal, digest) "
            "VALUES (?, ?, ?, ?)",
            [
                (node_id, relpath, ordinal, digest)
                for ordinal, digest in enumerate(chunk_digests)
            ],
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_chunk(self, digest: str) -> bytes:
        """The plaintext bytes of one chunk, decoded and digest-verified.

        Raises:
            IntegrityError: If the chunk is missing or of an unknown kind.
            CorruptChunkError: If the decoded bytes no longer match the
                digest they are stored under.
        """
        row = self._manager.read().execute(
            "SELECT kind, data FROM chunk WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise IntegrityError(
                f"Chunk {digest[:12]}… is referenced but missing from the "
                "pool; the store may be corrupt."
            )
        if row["kind"] != _KIND_RAW:
            raise IntegrityError(
                f"Chunk {digest[:12]}… has kind {row['kind']!r}, which this "
                "version cannot decode."
            )
        data = zlib.decompress(row["data"])
        if hashlib.sha256(data).hexdigest() != digest:
            raise CorruptChunkError(
                f"Chunk {digest[:12]}… failed its integrity check."
            )
        return data

    def has_artifacts(self, node_id: str) -> bool:
        row = self._manager.read().execute(
            "SELECT 1 FROM artifact WHERE node_id = ? LIMIT 1", (node_id,)
        ).fetchone()
        return row is not None

    def artifact_manifest(self, node_id: str) -> Dict[str, ArtifactRecord]:
        """Every artifact of a node, keyed by relpath, with its ordered
        chunk recipe. Empty for a node with no artifacts."""
        rows = self._manager.read().execute(
            "SELECT a.relpath, a.size, a.sha256, ac.digest "
            "FROM artifact a "
            "LEFT JOIN artifact_chunk ac "
            "  ON ac.node_id = a.node_id AND ac.relpath = a.relpath "
            "WHERE a.node_id = ? "
            "ORDER BY a.relpath, ac.ordinal",
            (node_id,),
        ).fetchall()
        grouped: Dict[str, Tuple[int, str, List[str]]] = {}
        for row in rows:
            entry = grouped.setdefault(
                row["relpath"], (row["size"], row["sha256"], [])
            )
            if row["digest"] is not None:  # an empty artifact has no chunks
                entry[2].append(row["digest"])
        return {
            relpath: ArtifactRecord(
                relpath=relpath,
                size=size,
                sha256=sha256,
                chunk_digests=tuple(digests),
            )
            for relpath, (size, sha256, digests) in grouped.items()
        }

    def artifact_bytes(self, node_id: str, relpath: str) -> bytes:
        """Reassembles one artifact in memory, verified end to end.

        Raises:
            ArtifactNotFound: If the node has no artifact at `relpath`.
            CorruptChunkError: If a chunk or the whole artifact fails its
                digest check.
        """
        conn = self._manager.read()
        artifact = conn.execute(
            "SELECT sha256 FROM artifact WHERE node_id = ? AND relpath = ?",
            (node_id, relpath),
        ).fetchone()
        if artifact is None:
            raise ArtifactNotFound(
                f"Node {node_id!r} has no artifact {relpath!r}."
            )
        digests = [
            row["digest"]
            for row in conn.execute(
                "SELECT digest FROM artifact_chunk "
                "WHERE node_id = ? AND relpath = ? ORDER BY ordinal",
                (node_id, relpath),
            )
        ]
        data = b"".join(self.get_chunk(digest) for digest in digests)
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise CorruptChunkError(
                f"Artifact {relpath!r} of node {node_id!r} failed its "
                "integrity check while being reassembled."
            )
        return data

    def reassemble(self, node_id: str, relpath: str) -> Path:
        """Reassembles one artifact into the session read cache and returns
        the readable path. A copy already reassembled this session is
        reused as-is; the write is atomic (unique temp + rename), so
        concurrent reassembles of the same artifact cannot tear."""
        out = self._cache_dir() / node_id / relpath
        if out.exists():
            return out
        data = self.artifact_bytes(node_id, relpath)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        return out

    # ------------------------------------------------------------------
    # Session read cache
    # ------------------------------------------------------------------

    def _cache_dir(self) -> Path:
        """The session cache directory, created on first packed read. It
        lives in the system temp dir (never under the store root), so a
        hard-killed session's leftovers are the OS temp-cleaner's problem."""
        if self._cache_root is None:
            self._cache_root = Path(tempfile.mkdtemp(prefix="ancestree-cache-"))
            atexit.register(self.clear_cache)
        return self._cache_root

    def clear_cache(self) -> None:
        """Deletes this session's reassembled copies. Idempotent; also runs
        at interpreter exit. Pure derived data — the next read regenerates
        anything needed from the chunk pool."""
        if self._cache_root is not None:
            shutil.rmtree(self._cache_root, ignore_errors=True)
            self._cache_root = None
