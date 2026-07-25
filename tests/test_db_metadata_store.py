"""Phase 2: MetadataStore writes, queries and lineage (issue #13).

Ports the query/lineage semantics of test_querying_and_search.py and
test_dag.py to the SQLite persistence layer: equality and predicate
search, DAG lineage ordering, children, most_recent and dedup lookup.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest

from ancestree.db.connection import ConnectionManager
from ancestree.db.metadata_store import (
    MetadataRow,
    MetadataStore,
    NodeRecord,
    metadata_row,
)
from ancestree.errors import IntegrityError, NodeNotFound


@pytest.fixture()
def store(tmp_path: Path) -> MetadataStore:
    return MetadataStore(ConnectionManager(tmp_path / "store.db"))


_EPOCH = 1_750_000_000.0


def _record(
    node_id: str,
    step_type: str = "step",
    generation: int = 0,
    parents: tuple[str, ...] = (),
    epoch_offset: float = 0.0,
    healthy: bool = True,
) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        step_type=step_type,
        generation=generation,
        created_utc="2026-07-08T00:00:00+00:00",
        created_epoch=_EPOCH + epoch_offset,
        healthy=healthy,
        parent_ids=parents,
    )


def _add(
    store: MetadataStore,
    node_id: str,
    step_type: str = "step",
    parents: tuple[str, ...] = (),
    epoch_offset: float = 0.0,
    healthy: bool = True,
    metadata: Sequence[MetadataRow] = (),
) -> None:
    store.add_node(
        _record(
            node_id,
            step_type=step_type,
            parents=parents,
            epoch_offset=epoch_offset,
            healthy=healthy,
        ),
        metadata,
    )


def test_roundtrip_record_and_metadata(store: MetadataStore) -> None:
    _add(store, "a")
    _add(
        store,
        "b",
        step_type="model",
        parents=("a",),
        epoch_offset=1,
        metadata=[
            metadata_row("accuracy", 0.94),
            metadata_row("notes", "rerun", searchable=False),
            metadata_row("params", {"depth": 3, "lr": 0.1}, data_type="json"),
        ],
    )

    record = store.get("b")
    assert record is not None
    assert record.step_type == "model"
    assert record.parent_ids == ("a",)
    assert record.healthy is True

    meta = store.metadata_for("b")
    assert meta["accuracy"].value == 0.94
    assert meta["accuracy"].num_value == 0.94
    assert meta["notes"].searchable is False
    assert meta["params"].value == {"depth": 3, "lr": 0.1}

    assert store.get("missing") is None
    assert store.exists("b") and not store.exists("missing")
    assert store.all_node_ids() == ["a", "b"]


def test_parent_order_is_preserved(store: MetadataStore) -> None:
    _add(store, "p1")
    _add(store, "p2", epoch_offset=1)
    _add(store, "join", parents=("p2", "p1"), epoch_offset=2)
    record = store.get("join")
    assert record is not None
    assert record.parent_ids == ("p2", "p1")


def test_find_by_column_equality(store: MetadataStore) -> None:
    _add(store, "a", step_type="ingest")
    _add(store, "b", step_type="clean", epoch_offset=1)
    _add(store, "c", step_type="clean", epoch_offset=2, healthy=False)

    assert store.find(step_type="clean") == ["b", "c"]
    assert store.find(step_type="clean", healthy=True) == ["b"]
    assert store.find(healthy=False) == ["c"]
    assert store.find(node_id="a") == ["a"]
    assert store.find(step_type="nope") == []


def test_find_returns_all_when_unfiltered(store: MetadataStore) -> None:
    _add(store, "a")
    _add(store, "b", epoch_offset=1)
    assert store.find() == ["a", "b"]


def test_find_by_metadata_equality(store: MetadataStore) -> None:
    _add(store, "a", metadata=[metadata_row("accuracy", 0.9)])
    _add(
        store,
        "b",
        epoch_offset=1,
        metadata=[metadata_row("accuracy", 0.5), metadata_row("tag", "best")],
    )

    assert store.find(accuracy=0.9) == ["a"]
    assert store.find(tag="best") == ["b"]
    assert store.find(accuracy=0.5, tag="best") == ["b"]
    assert store.find(accuracy=0.9, tag="best") == []
    # ints and floats compare numerically, as Python equality did in 0.1.x
    _add(store, "c", epoch_offset=2, metadata=[metadata_row("epochs", 10)])
    assert store.find(epochs=10.0) == ["c"]


def test_find_mixes_columns_and_metadata(store: MetadataStore) -> None:
    _add(store, "a", step_type="model", metadata=[metadata_row("acc", 0.9)])
    _add(
        store,
        "b",
        step_type="model",
        epoch_offset=1,
        metadata=[metadata_row("acc", 0.4)],
    )
    _add(store, "c", step_type="clean", epoch_offset=2)
    assert store.find(step_type="model", acc=0.9) == ["a"]


def test_find_with_predicates(store: MetadataStore) -> None:
    _add(store, "a", metadata=[metadata_row("accuracy", 0.9)])
    _add(store, "b", epoch_offset=1, metadata=[metadata_row("accuracy", 0.5)])
    _add(store, "c", epoch_offset=2)  # no accuracy at all

    # The predicate receives None for a node missing the key — the 0.1.x
    # contract that `lambda a: a and a > 0.8` relies on.
    assert store.find(accuracy=lambda a: a is not None and a > 0.8) == ["a"]
    assert store.find(accuracy=lambda a: a is None) == ["c"]
    # Predicates compose with SQL filters.
    assert store.find(
        step_type="step", accuracy=lambda a: a is not None and a < 0.8
    ) == ["b"]
    # Column predicates receive decoded values (healthy as bool).
    assert store.find(healthy=lambda h: h is True) == ["a", "b", "c"]


def test_raising_predicate_warns_and_excludes(store: MetadataStore) -> None:
    _add(store, "a", metadata=[metadata_row("accuracy", 0.9)])
    with pytest.warns(UserWarning, match="Predicate for 'accuracy' raised"):
        assert store.find(accuracy=lambda a: a.undefined) == []


def test_unsearchable_metadata_is_not_matched(store: MetadataStore) -> None:
    _add(store, "a", metadata=[metadata_row("secret", "x", searchable=False)])
    assert store.find(secret="x") == []
    # A predicate sees None for an unsearchable key, mirroring 0.1.x where
    # unsearchable entries were absent from the index entirely.
    assert store.find(secret=lambda v: v is None) == ["a"]


def test_most_recent(store: MetadataStore) -> None:
    _add(store, "old", step_type="clean")
    _add(store, "new", step_type="clean", epoch_offset=5)
    _add(store, "newest-other", step_type="model", epoch_offset=9)

    assert store.most_recent(store.find(step_type="clean")) == "new"
    assert store.most_recent([]) is None


def test_find_by_hash(store: MetadataStore) -> None:
    store.add_node(
        NodeRecord(
            node_id="a",
            step_type="step",
            generation=0,
            created_utc="t",
            created_epoch=_EPOCH,
            healthy=True,
            content_hash="cafe123",
        )
    )
    assert store.find_by_hash("cafe123") == "a"
    assert store.find_by_hash("beef456") is None


def test_children(store: MetadataStore) -> None:
    _add(store, "root")
    _add(store, "kid1", parents=("root",), epoch_offset=1)
    _add(store, "kid2", parents=("root",), epoch_offset=2)
    _add(store, "grandkid", parents=("kid1",), epoch_offset=3)

    assert store.children("root") == ["kid1", "kid2"]
    assert store.children("grandkid") == []


def test_lineage_linear_chain(store: MetadataStore) -> None:
    _add(store, "a")
    _add(store, "b", parents=("a",), epoch_offset=1)
    _add(store, "c", parents=("b",), epoch_offset=2)
    _add(store, "d", parents=("c",), epoch_offset=3)

    assert store.lineage("d") == ["a", "b", "c", "d"]
    assert store.lineage("a") == ["a"]


def test_lineage_diamond_join(store: MetadataStore) -> None:
    _add(store, "a")
    _add(store, "b", parents=("a",), epoch_offset=1)
    _add(store, "c", parents=("a",), epoch_offset=2)
    _add(store, "d", parents=("b", "c"), epoch_offset=3)

    order = store.lineage("d")
    assert sorted(order) == ["a", "b", "c", "d"]  # union, each node once
    assert order[0] == "a" and order[-1] == "d"  # after all parents
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_lineage_unknown_node_raises(store: MetadataStore) -> None:
    with pytest.raises(NodeNotFound):
        store.lineage("ghost")


def test_lineage_cycle_raises(store: MetadataStore, tmp_path: Path) -> None:
    _add(store, "a")
    _add(store, "b", parents=("a",), epoch_offset=1)
    # Corrupt the graph directly: make 'a' a child of 'b' (a -> b -> a).
    with store._manager.write() as conn:
        conn.execute("INSERT INTO edge VALUES ('a', 'b', 0)")
    with pytest.raises(IntegrityError, match="Cycle detected"):
        store.lineage("b")


def test_remove_is_a_noop_for_missing_and_cascades(
    store: MetadataStore,
) -> None:
    _add(store, "a", metadata=[metadata_row("k", 1)])
    _add(store, "b", parents=("a",), epoch_offset=1)

    store.remove("ghost")  # silently fine, as in 0.1.x
    store.remove("a")
    assert store.get("a") is None
    assert store.metadata_for("a") == {}
    record = store.get("b")
    assert record is not None and record.parent_ids == ()  # edge cascaded


def test_find_by_parent_id(store: MetadataStore) -> None:
    # 0.1.x indexed parent_id as a searchable list; the edge-backed store
    # must answer the same filters.
    _add(store, "root")
    _add(store, "other", epoch_offset=1)
    _add(store, "kid", parents=("root",), epoch_offset=2)
    _add(store, "join", parents=("root", "other"), epoch_offset=3)

    assert store.find(parent_id=[]) == ["root", "other"]  # roots
    assert store.find(parent_id=["root"]) == ["kid"]
    assert store.find(parent_id=["root", "other"]) == ["join"]  # ordered
    assert store.find(parent_id=["other", "root"]) == []
    assert store.find(parent_id=lambda p: len(p) > 1) == ["join"]
    assert store.find(step_type="step", parent_id=lambda p: "root" in p) == [
        "kid",
        "join",
    ]
