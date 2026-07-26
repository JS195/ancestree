"""Phase 5: reuse_identical (issue #16) — completes the goal of
the original feature branch on the new substrate. Ports the semantics of
test_dedupe.py: identical clean nodes merge (the handle rebinds), anything
differing stays separate, failed runs never merge, and the fingerprint is
only ever a candidate key."""

import hashlib
from pathlib import Path

import pytest

from ancestree.domain.fingerprint import ContentSummary
from ancestree.store import LineageStore


@pytest.fixture()
def store(tmp_path: Path) -> LineageStore:
    return LineageStore(tmp_path / "proj")  # reuse_identical defaults to True


def _run(
    store: LineageStore,
    payload: bytes = b"identical bytes",
    accuracy: float = 0.9,
    step_type: str = "ingest",
    parent: object = None,
):
    with store.create_node(step_type=step_type, parent=parent) as node:
        (node / "out.bin").write_bytes(payload)
        node.add_meta("accuracy", accuracy)
    return node


# ---------------------------------------------------------------------------
# The fingerprint itself
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_and_order_independent() -> None:
    meta = {
        "accuracy": {
            "value": 0.9,
            "data_type": "text",
            "group": "General",
            "searchable": True,
        }
    }
    arts = {"out.bin": "a" * 64}
    one = ContentSummary.of("ingest", ["p1", "p2"], meta, arts)
    two = ContentSummary.of("ingest", ["p2", "p1"], meta, arts)
    assert one == two and one.digest == two.digest  # parent order is not content

    assert ContentSummary.of("clean", ["p1", "p2"], meta, arts) != one
    assert ContentSummary.of("ingest", ["p1"], meta, arts).digest != one.digest
    assert ContentSummary.of("ingest", ["p1", "p2"], {}, arts).digest != one.digest
    assert ContentSummary.of("ingest", ["p1", "p2"], meta, {}).digest != one.digest


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------


def test_identical_clean_nodes_merge(store: LineageStore) -> None:
    first = _run(store)
    second = _run(store)  # content-identical rerun

    assert second.node_id == first.node_id  # the handle was rebound
    assert len(store.find()) == 1
    assert store.stats()["nodes"] == 1
    record = store.get(first)
    assert record is not None and record.content_hash is not None


def test_rebound_handle_works_as_a_parent(store: LineageStore) -> None:
    first = _run(store)
    duplicate = _run(store)
    with store.create_node(step_type="clean", parent=duplicate) as child:
        child.add_meta("ok", True)
    record = store.get(child)
    assert record is not None and record.parent_id == (first.node_id,)


def test_any_content_difference_keeps_nodes_separate(store: LineageStore) -> None:
    _run(store, payload=b"bytes A")
    _run(store, payload=b"bytes B")  # different artifact bytes
    _run(store, payload=b"bytes A", accuracy=0.5)  # different metadata
    assert len(store.find()) == 3


def test_parents_are_part_of_identity(store: LineageStore) -> None:
    root_a = _run(store, payload=b"root A")
    root_b = _run(store, payload=b"root B")
    _run(store, step_type="child", parent=root_a)
    _run(store, step_type="child", parent=root_b)  # same content, other parent
    assert len(store.find(step_type="child")) == 2


def test_failed_runs_never_merge(store: LineageStore) -> None:
    for _ in range(2):
        with pytest.raises(RuntimeError), store.create_node(step_type="ingest") as node:
            (node / "partial.bin").write_bytes(b"same partial bytes")
            raise RuntimeError("boom")
    unhealthy = store.find(healthy=False)
    assert len(unhealthy) == 2  # partial work is evidence, never merged
    assert all(record.content_hash is None for record in unhealthy)


def test_reuse_identical_off_keeps_duplicates(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "nodedup", reuse_identical=False)
    _run(store)
    _run(store)
    assert len(store.find()) == 2


def test_reuse_identical_works_across_reopen(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    first = LineageStore(root)
    original = _run(first)
    first.close()

    second = LineageStore(root)
    duplicate = _run(second)
    assert duplicate.node_id == original.node_id
    assert len(second.find()) == 1
    second.close()


def test_hash_collision_is_verified_not_trusted(store: LineageStore) -> None:
    decoy = _run(store, payload=b"decoy bytes", accuracy=0.1)

    # Forge a collision: stamp the decoy with the digest the NEXT run's
    # content will produce. find_by_hash will nominate the decoy, but the
    # full ContentSummary comparison must reject it.
    upcoming = ContentSummary.of(
        "ingest",
        [],
        {
            "accuracy": {
                "value": 0.9,
                "data_type": "text",
                "group": "General",
                "searchable": True,
            }
        },
        {"out.bin": hashlib.sha256(b"real bytes").hexdigest()},
    )
    with store._manager.write() as conn:
        conn.execute(
            "UPDATE node SET content_hash = ? WHERE node_id = ?",
            (upcoming.digest, decoy.node_id),
        )

    real = _run(store, payload=b"real bytes", accuracy=0.9)
    assert real.node_id != decoy.node_id  # never merged on hash alone
    assert len(store.find()) == 2
