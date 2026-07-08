"""ChunkStore — chunk BLOBs, artifact recipes, reassembly and the read cache.

The content-addressed pool lives in the ``chunk`` table: each chunk is
stored once, keyed by the SHA-256 of its plaintext — a repeat put is a
no-op, which is the exact dedup (Layer 1). With the store's ``chunk``
policy on, Layer 2 engages: a new chunk's super-features nominate a
similar raw chunk as a delta base, and the newcomer is stored as a
zlib-zdict delta (kind 1) when that genuinely beats plain compression.
Depth is capped at 1 — a base is always raw — so a read costs at most two
fetches and GC reachability is a single hop (AD5).

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
from ..ingest.cdc import delta_decode, delta_encode, super_features
from .connection import ConnectionManager

_KIND_RAW = 0
_KIND_DELTA = 1  # zlib-zdict against a raw base; depth capped at 1

# Layer-2 policy knobs: tiny chunks are not worth a trial encode, and a
# delta is kept only when it beats plain compression by a real margin.
_DELTA_MIN_SIZE = 2048
_DELTA_WIN = 0.8


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

    def __init__(self, manager: ConnectionManager, delta: bool = False) -> None:
        """`delta` is the store's persisted ``chunk`` policy: when True,
        Layer-2 resemblance/delta storage runs on every new chunk."""
        self._manager = manager
        self._delta = delta
        self._cache_root: Optional[Path] = None

    # ------------------------------------------------------------------
    # Writes (the caller holds the write transaction)
    # ------------------------------------------------------------------

    def put_chunk(self, conn: sqlite3.Connection, data: bytes) -> str:
        """Stores one plaintext chunk and returns its digest.

        A chunk already in the pool costs nothing — that is Layer 1, the
        exact dedup. Otherwise, with the delta policy on, the chunk's
        super-features nominate the most similar raw chunk as a base and a
        trial delta is kept only when it genuinely beats plain compression
        (Layer 2); everything else lands as a raw zlib chunk whose
        features join the resemblance index.
        """
        digest = hashlib.sha256(data).hexdigest()
        already = conn.execute(
            "SELECT 1 FROM chunk WHERE digest = ? LIMIT 1", (digest,)
        ).fetchone()
        if already is not None:
            return digest  # Layer 1: the second copy is free

        if self._delta and len(data) >= _DELTA_MIN_SIZE:
            features = super_features(data)
            if not self._try_delta(conn, digest, data, features):
                self._insert_chunk(
                    conn, digest, _KIND_RAW, None, zlib.compress(data), len(data)
                )
                self._store_features(conn, digest, features)
            return digest

        self._insert_chunk(
            conn, digest, _KIND_RAW, None, zlib.compress(data), len(data)
        )
        return digest

    def _try_delta(
        self,
        conn: sqlite3.Connection,
        digest: str,
        data: bytes,
        features: Sequence[int],
    ) -> bool:
        """Stores `data` as a delta if a similar raw base exists AND the
        delta beats plain compression by the policy margin. A nominated
        candidate that encodes poorly just costs the trial — correctness
        never depends on the resemblance index being right."""
        base_digest = self._find_base(conn, features)
        if base_digest is None:
            return False
        base = self.get_chunk(base_digest)
        delta_blob = delta_encode(base, data)
        if len(delta_blob) >= len(zlib.compress(data)) * _DELTA_WIN:
            return False
        self._insert_chunk(
            conn, digest, _KIND_DELTA, base_digest, delta_blob, len(data)
        )
        return True

    def _find_base(
        self, conn: sqlite3.Connection, features: Sequence[int]
    ) -> Optional[str]:
        """The raw chunk sharing the most super-features with the newcomer.
        Only raw chunks qualify — that is what caps delta depth at 1."""
        if not features:
            return None
        placeholders = ",".join("?" for _ in features)
        row = conn.execute(
            "SELECT cf.digest FROM chunk_feature cf "
            f"JOIN chunk c ON c.digest = cf.digest AND c.kind = {_KIND_RAW} "
            f"WHERE cf.feature IN ({placeholders}) "
            "GROUP BY cf.digest "
            "ORDER BY count(*) DESC, c.created_epoch LIMIT 1",
            list(features),
        ).fetchone()
        return None if row is None else str(row["digest"])

    def _store_features(
        self, conn: sqlite3.Connection, digest: str, features: Sequence[int]
    ) -> None:
        conn.executemany(
            "INSERT OR IGNORE INTO chunk_feature (feature, digest) "
            "VALUES (?, ?)",
            [(feature, digest) for feature in features],
        )

    @staticmethod
    def _insert_chunk(
        conn: sqlite3.Connection,
        digest: str,
        kind: int,
        base_digest: Optional[str],
        blob: bytes,
        length: int,
    ) -> None:
        conn.execute(
            "INSERT INTO chunk "
            "(digest, kind, base_digest, data, length, created_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (digest, kind, base_digest, blob, length, time.time()),
        )

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
        For a delta chunk this fetches and verifies its raw base too —
        depth is capped at 1, so a read is never more than two fetches.

        Raises:
            IntegrityError: If the chunk (or its base) is missing, of an
                unknown kind, or chained deeper than depth 1.
            CorruptChunkError: If decoded bytes no longer match the digest
                they are stored under.
        """
        data = self._decode_chunk(digest, allow_delta=True)
        if hashlib.sha256(data).hexdigest() != digest:
            raise CorruptChunkError(
                f"Chunk {digest[:12]}… failed its integrity check."
            )
        return data

    def _decode_chunk(self, digest: str, allow_delta: bool) -> bytes:
        row = self._manager.read().execute(
            "SELECT kind, base_digest, data FROM chunk WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise IntegrityError(
                f"Chunk {digest[:12]}… is referenced but missing from the "
                "pool; the store may be corrupt."
            )
        if row["kind"] == _KIND_RAW:
            return zlib.decompress(row["data"])
        if row["kind"] == _KIND_DELTA:
            if not allow_delta:
                raise IntegrityError(
                    f"Chunk {digest[:12]}… is a delta whose base is itself "
                    "a delta; depth is capped at 1 and the store may be "
                    "corrupt."
                )
            base_digest = row["base_digest"]
            base = self._decode_chunk(base_digest, allow_delta=False)
            if hashlib.sha256(base).hexdigest() != base_digest:
                raise CorruptChunkError(
                    f"Chunk {str(base_digest)[:12]}… (a delta base) failed "
                    "its integrity check."
                )
            return delta_decode(base, row["data"])
        raise IntegrityError(
            f"Chunk {digest[:12]}… has kind {row['kind']!r}, which this "
            "version cannot decode."
        )

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
