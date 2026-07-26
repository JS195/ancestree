"""Phase 3: NodeWorkspace and the synchronous ingest pipeline (issue #14).

The exit criterion made executable: artifacts written to scratch round-trip
through SQLite byte-for-byte, in one atomic transaction, and the scratch
directory disappears on success (and survives on failure)."""

import json
import sqlite3
from pathlib import Path

import pytest

from ancestree.db.chunk_store import ChunkStore
from ancestree.db.connection import ConnectionManager
from ancestree.db.metadata_store import (
    MetadataStore,
    NodeRecord,
    metadata_row,
)
from ancestree.ingest.packing import ingest_node
from ancestree.ingest.workspace import SEED_FILENAME, NodeWorkspace

Env = tuple[ConnectionManager, MetadataStore, ChunkStore]


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    manager = ConnectionManager(tmp_path / "store.db")
    return manager, MetadataStore(manager), ChunkStore(manager)


def _record(node_id: str, parents: tuple[str, ...] = ()) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        step_type="clean",
        generation=0,
        created_utc="2026-07-08T00:00:00+00:00",
        created_epoch=1_750_000_000.0,
        healthy=True,
        parent_id=parents,
    )


def _chunk_count(manager: ConnectionManager) -> int:
    row = manager.read().execute("SELECT count(*) AS n FROM chunk").fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# NodeWorkspace
# ---------------------------------------------------------------------------


def test_workspace_creates_scratch_and_seed(tmp_path: Path) -> None:
    ws = NodeWorkspace(tmp_path, "abc12345", "clean", ("p1", "p2"))
    assert ws.path == tmp_path / ".scratch" / "abc12345"
    assert ws.path.is_dir()

    seed = json.loads((ws.path / SEED_FILENAME).read_text())
    assert seed["node_id"] == "abc12345"
    assert seed["step_type"] == "clean"
    assert seed["parent_id"] == ["p1", "p2"]
    assert isinstance(seed["pid"], int)
    assert "started_utc" in seed


def test_workspace_resolve_makes_parents_and_guards(tmp_path: Path) -> None:
    ws = NodeWorkspace(tmp_path, "abc12345", "clean")
    target = ws.resolve("results/deep/out.csv")
    assert target.parent.is_dir()
    assert target == ws.path / "results" / "deep" / "out.csv"

    with pytest.raises(ValueError, match="escapes"):
        ws.resolve("../escape.txt")
    with pytest.raises(ValueError, match="reserved"):
        ws.resolve(SEED_FILENAME)


def test_workspace_files_excludes_seed_and_sorts(tmp_path: Path) -> None:
    ws = NodeWorkspace(tmp_path, "abc12345", "clean")
    ws.resolve("b.txt").write_text("b")
    ws.resolve("a/nested.txt").write_text("n")
    listed = ws.files()
    assert [rel for rel, _ in listed] == ["a/nested.txt", "b.txt"]

    ws.discard()
    assert not ws.path.exists()


def test_workspace_files_skips_symlinks_escaping_the_node(tmp_path: Path) -> None:
    """A symlink must not be a way around resolve()'s containment guard.

    ``resolve("../x")`` raises, so ingesting the target of a link pointing
    the same way would silently pull an outside file into the store.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET")

    ws = NodeWorkspace(tmp_path / "store", "abc12345", "clean")
    ws.resolve("real.txt").write_text("real")
    (ws.path / "leak.txt").symlink_to(outside / "secret.txt")

    with pytest.warns(UserWarning, match="pointing outside"):
        listed = ws.files()
    assert [rel for rel, _ in listed] == ["real.txt"]


def test_workspace_files_follows_symlinks_inside_the_node(tmp_path: Path) -> None:
    """Containment is the rule, not symlink-ness: a link to the node's own
    content is a legitimate artifact and is still ingested."""
    ws = NodeWorkspace(tmp_path / "store", "abc12345", "clean")
    ws.resolve("data/real.txt").write_text("real")
    (ws.path / "alias.txt").symlink_to(ws.path / "data" / "real.txt")

    listed = ws.files()
    assert [rel for rel, _ in listed] == ["alias.txt", "data/real.txt"]
    assert dict(listed)["alias.txt"].read_text() == "real"


def test_workspace_files_ignores_broken_and_dangling_symlinks(tmp_path: Path) -> None:
    ws = NodeWorkspace(tmp_path / "store", "abc12345", "clean")
    ws.resolve("real.txt").write_text("real")
    (ws.path / "broken.txt").symlink_to(tmp_path / "nope" / "gone.txt")

    assert [rel for rel, _ in ws.files()] == ["real.txt"]


# ---------------------------------------------------------------------------
# ingest_node
# ---------------------------------------------------------------------------


def test_ingest_roundtrips_artifacts_and_deletes_scratch(
    env: Env, tmp_path: Path
) -> None:
    manager, metadata_store, chunk_store = env
    ws = NodeWorkspace(tmp_path, "n1", "clean")
    small = b"col_a,col_b\n1,2\n"
    import random

    big = random.Random(11).randbytes(500_000)
    ws.resolve("table.csv").write_bytes(small)
    ws.resolve("blobs/model.bin").write_bytes(big)

    result = ingest_node(
        manager,
        metadata_store,
        chunk_store,
        ws,
        _record("n1"),
        [metadata_row("accuracy", 0.93)],
    )

    assert result.record.size_bytes == len(small) + len(big)
    stored = metadata_store.get("n1")
    assert stored is not None and stored.size_bytes == result.record.size_bytes
    assert metadata_store.metadata_for("n1")["accuracy"].value == 0.93

    manifest = chunk_store.artifact_manifest("n1")
    assert set(manifest) == {"table.csv", "blobs/model.bin"}
    assert chunk_store.artifact_bytes("n1", "table.csv") == small
    assert chunk_store.artifact_bytes("n1", "blobs/model.bin") == big
    assert set(result.artifact_sha256s) == set(manifest)

    assert not ws.path.exists()  # scratch reclaimed after the commit


def test_ingest_metadata_only_node(env: Env, tmp_path: Path) -> None:
    manager, metadata_store, chunk_store = env
    ws = NodeWorkspace(tmp_path, "n1", "note")
    result = ingest_node(
        manager,
        metadata_store,
        chunk_store,
        ws,
        _record("n1"),
        [metadata_row("note", "no files, still a node")],
    )
    assert result.record.size_bytes == 0
    assert metadata_store.exists("n1")
    assert not chunk_store.has_artifacts("n1")


def test_failed_ingest_rolls_back_and_keeps_scratch(env: Env, tmp_path: Path) -> None:
    manager, metadata_store, chunk_store = env
    metadata_store.add_node(_record("dup"))  # occupy the id

    ws = NodeWorkspace(tmp_path, "dup", "clean")
    ws.resolve("out.bin").write_bytes(b"x" * 100_000)
    with pytest.raises(sqlite3.IntegrityError):
        ingest_node(manager, metadata_store, chunk_store, ws, _record("dup"))

    assert _chunk_count(manager) == 0  # nothing leaked
    assert metadata_store.metadata_for("dup") == {}
    assert ws.path.exists()  # partial work is preserved for inspection


def test_composed_write_is_atomic_across_stores(env: Env, tmp_path: Path) -> None:
    # The reentrant-transaction property ingest relies on, demonstrated at
    # the store level: chunks and the node row vanish together on failure.
    manager, metadata_store, chunk_store = env
    with pytest.raises(RuntimeError, match="boom"), manager.write() as conn:
        chunk_store.put_chunk(conn, b"y" * 50_000)
        metadata_store.add_node(_record("doomed"))
        raise RuntimeError("boom")
    assert _chunk_count(manager) == 0
    assert not metadata_store.exists("doomed")
