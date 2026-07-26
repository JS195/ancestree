"""Phase 3: the SQLite chunk pool — exact dedup, artifact recipes,
verified reassembly and the system-temp read cache (issue #14)."""

import zlib
from pathlib import Path

import pytest

from ancestree.db.chunk_store import ChunkStore
from ancestree.db.connection import ConnectionManager
from ancestree.db.metadata_store import MetadataStore, NodeRecord
from ancestree.errors import ArtifactNotFound, CorruptChunkError, IntegrityError
from ancestree.ingest.cdc import chunk_bytes

Env = tuple[ConnectionManager, MetadataStore, ChunkStore]


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    manager = ConnectionManager(tmp_path / "store.db")
    return manager, MetadataStore(manager), ChunkStore(manager)


def _add_node(metadata_store: MetadataStore, node_id: str) -> None:
    metadata_store.add_node(
        NodeRecord(
            node_id=node_id,
            step_type="step",
            generation=0,
            created_utc="t",
            created_epoch_seconds=0.0,
            healthy=True,
        )
    )


def _random_bytes(n: int) -> bytes:
    import random

    return random.Random(7).randbytes(n)


def _store_file(env: Env, node_id: str, relpath: str, data: bytes) -> None:
    manager, _metadata_store, chunk_store = env
    import hashlib

    with manager.write() as conn:
        digests = [chunk_store.put_chunk(conn, c) for c in chunk_bytes(data)]
        chunk_store.add_artifact(
            conn,
            node_id,
            relpath,
            len(data),
            hashlib.sha256(data).hexdigest(),
            digests,
        )


def _chunk_count(manager: ConnectionManager) -> int:
    row = manager.read().execute("SELECT count(*) AS n FROM chunk").fetchone()
    return int(row["n"])


def test_put_chunk_roundtrips_and_deduplicates(env: Env) -> None:
    manager, _, chunk_store = env
    data = b"hello world" * 5000
    with manager.write() as conn:
        first = chunk_store.put_chunk(conn, data)
        second = chunk_store.put_chunk(conn, data)
    assert first == second
    assert _chunk_count(manager) == 1  # stored once: that IS the dedup
    assert chunk_store.get_chunk(first) == data


def test_get_missing_chunk_raises(env: Env) -> None:
    _, _, chunk_store = env
    with pytest.raises(IntegrityError, match="missing"):
        chunk_store.get_chunk("0" * 64)


def test_corrupt_chunk_is_detected(env: Env) -> None:
    manager, _, chunk_store = env
    with manager.write() as conn:
        digest = chunk_store.put_chunk(conn, b"precious bytes" * 1000)
    with manager.write() as conn:
        conn.execute(
            "UPDATE chunk SET data = ? WHERE digest = ?",
            (zlib.compress(b"evil bytes"), digest),
        )
    with pytest.raises(CorruptChunkError):
        chunk_store.get_chunk(digest)


def test_artifact_recipe_roundtrips(env: Env) -> None:
    _manager, metadata_store, chunk_store = env
    _add_node(metadata_store, "n1")
    data = _random_bytes(600_000)  # several chunks
    _store_file(env, "n1", "results/out.bin", data)

    assert chunk_store.has_artifacts("n1")
    assert not chunk_store.has_artifacts("n2")

    manifest = chunk_store.artifact_manifest("n1")
    record = manifest["results/out.bin"]
    assert record.size == len(data)
    assert len(record.chunk_digests) > 1
    assert chunk_store.artifact_bytes("n1", "results/out.bin") == data


def test_empty_artifact_roundtrips(env: Env) -> None:
    _, metadata_store, chunk_store = env
    _add_node(metadata_store, "n1")
    _store_file(env, "n1", "empty.txt", b"")
    record = chunk_store.artifact_manifest("n1")["empty.txt"]
    assert record.size == 0 and record.chunk_digests == ()
    assert chunk_store.artifact_bytes("n1", "empty.txt") == b""


def test_identical_files_share_chunks_across_nodes(env: Env) -> None:
    manager, metadata_store, chunk_store = env
    _add_node(metadata_store, "n1")
    _add_node(metadata_store, "n2")
    data = _random_bytes(400_000)
    _store_file(env, "n1", "a.bin", data)
    before = _chunk_count(manager)
    _store_file(env, "n2", "b.bin", data)
    assert _chunk_count(manager) == before  # second copy cost zero chunks
    assert chunk_store.artifact_bytes("n2", "b.bin") == data


def test_reassemble_lands_in_the_store_cache(env: Env, tmp_path: Path) -> None:
    _, metadata_store, chunk_store = env
    _add_node(metadata_store, "n1")
    data = _random_bytes(100_000)
    _store_file(env, "n1", "out.bin", data)

    path = chunk_store.reassemble("n1", "out.bin")
    assert path.read_bytes() == data
    # The cache lives inside the store, in a pid-tagged session directory,
    # so returned paths read like they belong to the store.
    assert path.is_relative_to(tmp_path / ".cache")
    session = path.relative_to(tmp_path / ".cache").parts[0]
    assert session.split("-", 1)[0].isdigit()  # the pid the sweep checks
    # A second read reuses the same session copy.
    assert chunk_store.reassemble("n1", "out.bin") == path

    chunk_store.clear_cache()
    assert not path.exists()
    # The last session out also removes the shared .cache parent.
    assert not (tmp_path / ".cache").exists()


def test_missing_artifact_raises(env: Env) -> None:
    _, metadata_store, chunk_store = env
    _add_node(metadata_store, "n1")
    with pytest.raises(ArtifactNotFound):
        chunk_store.artifact_bytes("n1", "ghost.bin")


def test_tampered_artifact_digest_is_detected(env: Env) -> None:
    manager, metadata_store, chunk_store = env
    _add_node(metadata_store, "n1")
    _store_file(env, "n1", "out.bin", _random_bytes(50_000))
    with manager.write() as conn:
        conn.execute("UPDATE artifact SET sha256 = ? WHERE node_id = 'n1'", ("0" * 64,))
    with pytest.raises(CorruptChunkError):
        chunk_store.artifact_bytes("n1", "out.bin")
