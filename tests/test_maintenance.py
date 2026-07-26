"""Phase 5: prune, compact and the orphan-scratch sweep (issue #16)."""

import json
import os
from pathlib import Path

import pytest

from ancestree.domain.node import Node
from ancestree.ingest.workspace import SEED_FILENAME, NodeWorkspace
from ancestree.store import LineageStore


@pytest.fixture()
def store(tmp_path: Path) -> LineageStore:
    # reuse_identical off so identical payloads make distinct nodes.
    return LineageStore(tmp_path / "proj", reuse_identical=False)


def _node(
    store: LineageStore, step: str, parent: object = None, blob: bytes = b"x"
) -> Node:
    with store.create_node(step_type=step, parent=parent) as handle:
        (handle / "out.bin").write_bytes(blob)
    record = store.get(handle)
    assert record is not None
    return record


def _chunk_count(store: LineageStore) -> int:
    return int(store.sql("SELECT count(*) AS n FROM chunk")[0]["n"])


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def test_prune_dry_run_previews_without_deleting(store: LineageStore) -> None:
    a = _node(store, "a", blob=b"a")
    b = _node(store, "b", parent=a, blob=b"b")

    doomed = store.prune(a)  # dry_run defaults to True
    assert {node.node_id for node in doomed} == {a.node_id, b.node_id}
    assert len(store.find()) == 2  # nothing was actually removed


def test_prune_deletes_subtree_deepest_first(store: LineageStore) -> None:
    a = _node(store, "a", blob=b"a")
    b = _node(store, "b", parent=a, blob=b"b")
    c = _node(store, "c", parent=b, blob=b"c")

    deleted = store.prune(b, dry_run=False)
    assert [node.node_id for node in deleted] == [c.node_id, b.node_id]
    assert store.find() == [a]
    assert store.children(a) == []


def test_prune_spares_children_with_surviving_parents(
    store: LineageStore,
) -> None:
    p1 = _node(store, "p1", blob=b"p1")
    p2 = _node(store, "p2", blob=b"p2")
    child = _node(store, "join", parent=[p1, p2], blob=b"j")

    deleted = store.prune(p1, dry_run=False)
    assert [node.node_id for node in deleted] == [p1.node_id]

    survivor = store.get(child.node_id)
    assert survivor is not None
    # The edge to the pruned parent vanished via the FK cascade.
    assert survivor.parent_id == (p2.node_id,)


def test_prune_diamond_from_root_takes_everything(store: LineageStore) -> None:
    root = _node(store, "root", blob=b"r")
    left = _node(store, "left", parent=root, blob=b"l")
    right = _node(store, "right", parent=root, blob=b"rt")
    _node(store, "join", parent=[left, right], blob=b"j")

    deleted = store.prune(root, dry_run=False)
    assert len(deleted) == 4
    assert store.find() == []


def test_prune_unknown_node_is_empty(store: LineageStore) -> None:
    assert store.prune("deadbeef") == []
    assert store.prune(None) == []


# ---------------------------------------------------------------------------
# compact
# ---------------------------------------------------------------------------


def test_compact_reclaims_orphaned_chunks(store: LineageStore) -> None:
    import random

    keep = _node(store, "keep", blob=random.Random(1).randbytes(150_000))
    doomed = _node(store, "doomed", blob=random.Random(2).randbytes(150_000))

    # prune compacts by default; defer it so this test can exercise compact.
    store.prune(doomed, dry_run=False, compact=False)
    before = _chunk_count(store)
    assert before > 0  # orphans linger until compact

    removed = store.compact()
    assert removed > 0
    assert _chunk_count(store) == before - removed
    # The survivor's artifact still reads perfectly.
    assert len((keep / "out.bin").read_bytes()) == 150_000
    assert store.compact() == 0  # idempotent


def test_compact_keeps_chunks_shared_with_survivors(
    store: LineageStore,
) -> None:
    import random

    payload = random.Random(3).randbytes(150_000)
    keeper = _node(store, "keeper", blob=payload)
    twin = _node(store, "twin", blob=payload)  # reuse off: two nodes, shared chunks

    store.prune(twin, dry_run=False)
    assert store.compact() == 0  # every chunk still referenced by the keeper
    assert (keeper / "out.bin").read_bytes() == payload


# ---------------------------------------------------------------------------
# orphan-scratch sweep
# ---------------------------------------------------------------------------


def test_sweep_adopts_dead_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    workspace = NodeWorkspace(root, "orphan01", "clean", ("p1",), generation=2)
    workspace.resolve("partial.csv").write_text("half-written evidence")
    # 'p1' does not exist, which would fail adoption — drop it from the seed
    # to keep this test about the happy path.
    seed_path = workspace.path / SEED_FILENAME
    seed = json.loads(seed_path.read_text())
    seed["parent_ids"] = []
    seed_path.write_text(json.dumps(seed))

    monkeypatch.setattr("ancestree.maintenance._pid_alive", lambda pid: False)
    with pytest.warns(UserWarning, match="Adopted 1 orphaned"):
        store = LineageStore(root)

    record = store.get("orphan01")
    assert record is not None
    assert record.healthy is False  # a hard kill is never a clean run
    assert record.step_type == "clean"
    assert record.generation == 2
    assert (record / "partial.csv").read_text() == "half-written evidence"
    assert not workspace.path.exists()  # scratch fully reclaimed


def test_sweep_never_touches_live_sessions(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    workspace = NodeWorkspace(root, "live0001", "clean")  # seeded with OUR pid
    workspace.resolve("in-progress.csv").write_text("still being written")

    store = LineageStore(root)  # no monkeypatch: the owner (us) is alive
    assert store.get("live0001") is None
    assert workspace.path.exists()


def test_sweep_discards_litter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    (root / ".scratch" / "unseeded").mkdir(parents=True)
    (root / ".scratch" / "unseeded" / "junk.txt").write_text("x")
    empty = NodeWorkspace(root, "emptyone", "clean")  # seeded, never written

    monkeypatch.setattr("ancestree.maintenance._pid_alive", lambda pid: False)
    store = LineageStore(root)

    assert store.find() == []  # nothing adopted
    assert not (root / ".scratch" / "unseeded").exists()
    assert not empty.path.exists()


def test_sweep_cleans_committed_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    first = LineageStore(root)
    node = _node(first, "clean")
    first.close()

    # Simulate a crash after the commit but before the scratch cleanup.
    leftovers = NodeWorkspace(root, node.node_id, "clean")
    leftovers.resolve("stale.txt").write_text("already durable elsewhere")

    monkeypatch.setattr("ancestree.maintenance._pid_alive", lambda pid: False)
    second = LineageStore(root)  # no adoption warning expected

    assert len(second.find()) == 1  # no duplicate appeared
    assert not leftovers.path.exists()


def test_sweep_reaps_dead_cache_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    stale = root / ".cache" / "424242-deadbeef"
    stale.mkdir(parents=True)
    (stale / "old.bin").write_bytes(b"stale derived data")
    (root / ".cache" / "not-a-session").mkdir()  # litter: not pid-tagged

    monkeypatch.setattr("ancestree.maintenance._pid_alive", lambda pid: False)
    store = LineageStore(root)
    assert not stale.exists()
    assert not (root / ".cache" / "not-a-session").exists()
    store.close()


def test_sweep_keeps_live_cache_sessions(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    live = root / ".cache" / f"{os.getpid()}-abc12345"
    live.mkdir(parents=True)
    (live / "in-use.bin").write_bytes(b"another session is reading this")

    store = LineageStore(root)  # our pid is alive: never touched
    assert live.exists()
    store.close()
