"""Phase 4: the LineageStore facade — the redesigned public API (issue #15).

Rewrites the store-level coverage of test_store_api.py and
test_node_creation_edge_cases.py against the new backend: creation and
crash semantics, rules and generations, the query vocabulary, persisted
policy, and the sql/stats power tools.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from ancestree.domain.node import Node
from ancestree.errors import InvalidTransition, NodeNotFound, SchemaError
from ancestree.store import LineageStore


@pytest.fixture()
def store(tmp_path: Path) -> LineageStore:
    return LineageStore(
        tmp_path / "proj",
        rules={"clean": ["ingest"], "model": ["clean"]},
        gen_triggers=["ingest"],
    )


def _ingest(store: LineageStore, payload: bytes = b"raw,data\n1,2\n") -> Node:
    with store.create_node(step_type="ingest") as node:
        (node / "raw.csv").write_bytes(payload)
        node.add_meta("rows", 1)
    resolved = store.get(node)
    assert resolved is not None
    return resolved


# ---------------------------------------------------------------------------
# Creation & crash semantics
# ---------------------------------------------------------------------------


def test_create_node_persists_everything(store: LineageStore, tmp_path: Path) -> None:
    with store.create_node(step_type="ingest") as node:
        (node / "raw.csv").write_text("a,b\n1,2\n")
        (node / "figs/plot.txt").write_text("plot")
        node.add_meta("rows", 1, group="Metrics")

    record = store.get(node.node_id)
    assert record is not None
    assert record.step_type == "ingest"
    assert record.healthy is True
    assert record.duration_seconds is not None and record.duration_seconds >= 0
    assert record.size_bytes == len("a,b\n1,2\n") + len("plot")
    assert record.parent_id == ()
    assert record.metadata["rows"]["value"] == 1

    [figs, raw] = record.artifacts()
    assert raw.read_text() == "a,b\n1,2\n"
    assert figs.read_text() == "plot"

    # Nodes are rows, not folders: at rest the root holds only the
    # database (plus the empty .scratch parent).
    root = tmp_path / "proj"
    assert not (root / node.node_id).exists()
    assert list((root / ".scratch").iterdir()) == []
    names = {p.name for p in root.iterdir()}
    assert names <= {
        "ancestree.db",
        "ancestree.db-wal",
        "ancestree.db-shm",
        ".scratch",
        ".cache",  # the session read cache (reassembled artifact copies)
    }


def test_untouched_node_is_discarded_with_warning(store: LineageStore) -> None:
    with (
        pytest.warns(UserWarning, match="was discarded"),
        store.create_node(step_type="ingest") as node,
    ):
        pass
    assert store.get(node.node_id) is None
    assert store.find() == []


def test_metadata_only_node_persists(store: LineageStore) -> None:
    with store.create_node(step_type="ingest") as node:
        node.add_meta("note", "no files, still worth keeping")
    record = store.get(node.node_id)
    assert record is not None and record.size_bytes == 0


def test_exception_keeps_partial_work_as_unhealthy(store: LineageStore) -> None:
    with (
        pytest.raises(RuntimeError, match="mid-run failure"),
        store.create_node(step_type="ingest") as node,
    ):
        (node / "partial.csv").write_text("half-written")
        raise RuntimeError("mid-run failure")

    record = store.get(node.node_id)
    assert record is not None
    assert record.healthy is False  # partial work is evidence, not garbage
    assert (record / "partial.csv").read_text() == "half-written"
    assert store.find(healthy=False) == [record]


# ---------------------------------------------------------------------------
# Rules & generations
# ---------------------------------------------------------------------------


def test_rules_are_enforced(store: LineageStore) -> None:
    ingest = _ingest(store)
    with store.create_node(step_type="clean", parent=ingest) as node:
        node.add_meta("ok", True)

    with (
        pytest.raises(InvalidTransition),
        store.create_node(step_type="model", parent=ingest),
    ):
        pass
    # A rules-listed step type cannot be a root either.
    with pytest.raises(InvalidTransition), store.create_node(step_type="clean"):
        pass


def test_step_type_is_validated(store: LineageStore) -> None:
    with (
        pytest.raises(ValueError, match="non-empty"),
        store.create_node(step_type="   "),
    ):
        pass
    with (
        pytest.raises(ValueError, match="printable"),
        store.create_node(step_type="bad\nlabel"),
    ):
        pass


def test_gen_triggers_increment_generations(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "sim", gen_triggers=["sim"])
    with store.create_node(step_type="sim") as root:
        root.add_meta("seeded", True)
    assert root.generation == 0  # a root has no parents to advance from

    with store.create_node(step_type="sim", parent=root) as child:
        child.add_meta("seeded", True)
    assert child.generation == 1  # trigger + parent -> new generation

    with store.create_node(step_type="analyse", parent=child) as side:
        side.add_meta("ok", True)
    assert side.generation == 1  # non-trigger stays at its parent's depth


def test_unknown_parent_is_rejected(store: LineageStore) -> None:
    with (
        pytest.raises(ValueError, match="not present in this store"),
        store.create_node(step_type="ingest", parent="deadbeef"),
    ):
        pass


def test_parents_accept_handles_ids_and_records(store: LineageStore) -> None:
    ingest = _ingest(store)
    # by record, by id string, duplicates de-duplicated, order preserved
    with store.create_node(step_type="clean", parent=[ingest, ingest.node_id]) as node:
        node.add_meta("ok", True)
    record = store.get(node)
    assert record is not None and record.parent_id == (ingest.node_id,)

    other = _ingest(store, payload=b"other")
    with store.create_node(step_type="clean", parent=[other, ingest]) as join:
        join.add_meta("ok", True)
    joined = store.get(join)
    assert joined is not None
    assert joined.parent_id == (other.node_id, ingest.node_id)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_query_vocabulary(store: LineageStore) -> None:
    ingest = _ingest(store)
    with store.create_node(step_type="clean", parent=ingest) as clean_handle:
        (clean_handle / "clean.csv").write_text("clean")
        clean_handle.add_meta("accuracy", 0.91)
    with store.create_node(step_type="model", parent=clean_handle) as model_handle:
        model_handle.add_meta("accuracy", 0.97)

    clean = store.get(clean_handle)
    model = store.get(model_handle)
    assert clean is not None and model is not None

    assert store.find(step_type="clean") == [clean]
    assert store.find(accuracy=lambda a: a is not None and a > 0.95) == [model]
    assert store.latest(step_type="ingest") == ingest
    assert store.children(ingest) == [clean]
    assert store.lineage(model) == [ingest, clean, model]
    assert store.ancestors(model, accuracy=lambda a: a is not None) == [
        clean,
        model,
    ]
    with pytest.raises(NodeNotFound):
        store.lineage("deadbeef")

    assert store.get(None) is None
    assert store.get("none") is None
    assert store.get("missing1") is None
    assert store.children("missing1") == []


def test_from_parent_inside_a_block(store: LineageStore) -> None:
    _ingest(store)
    parent = store.latest(step_type="ingest")
    assert parent is not None
    with store.create_node(step_type="clean", parent=parent) as node:
        [raw] = store.from_parent(node, "raw.csv")
        assert raw.read_bytes() == b"raw,data\n1,2\n"
        (node / "clean.csv").write_text("ok")
    assert store.from_parent("missing1", "x") == []


# ---------------------------------------------------------------------------
# Persisted policy
# ---------------------------------------------------------------------------


def test_policy_is_persisted_and_immutable(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    first = LineageStore(root, rules={"clean": ["ingest"]}, reuse_identical=False)
    assert first.reuse_identical is False and first.delta is True
    first.close()

    # Reopening with nothing re-supplied uses the stored policy.
    second = LineageStore(root)
    assert second.rules == {"clean": ["ingest"]}
    assert second.reuse_identical is False
    second.close()

    with pytest.warns(UserWarning, match="Rules cannot be changed"):
        third = LineageStore(root, rules={"other": ["thing"]})
    assert third.rules == {"clean": ["ingest"]}
    third.close()

    with pytest.warns(UserWarning, match="persisted store policy"):
        fourth = LineageStore(root, reuse_identical=True)
    assert fourth.reuse_identical is False
    fourth.close()


# ---------------------------------------------------------------------------
# Power tools
# ---------------------------------------------------------------------------


def test_sql_is_read_only(store: LineageStore) -> None:
    _ingest(store)
    rows = store.sql("SELECT step_type, count(*) AS n FROM node GROUP BY step_type")
    assert [(row["step_type"], row["n"]) for row in rows] == [("ingest", 1)]
    rows = store.sql("SELECT node_id FROM node WHERE step_type = ?", ["ingest"])
    assert len(rows) == 1

    with pytest.raises(sqlite3.OperationalError):
        store.sql("INSERT INTO config VALUES ('evil', 'x')")


def test_stats_show_storage_dedup(store: LineageStore) -> None:
    import random

    payload = random.Random(3).randbytes(300_000)
    # Distinct metadata keeps the two nodes separate under
    # reuse_identical (Phase 5); identical bytes still share every chunk.
    for run in range(2):
        with store.create_node(step_type="ingest") as node:
            (node / "same.bin").write_bytes(payload)
            node.add_meta("run", run)

    stats = store.stats()
    assert stats["nodes"] == 2
    assert stats["artifacts"] == 2
    assert stats["chunks"] > 0
    assert stats["artifact_bytes"] == 2 * len(payload)
    # The second copy cost no new chunks, so artifact > stored.
    assert stats["dedup_ratio"] is not None and stats["dedup_ratio"] > 1.5
    assert stats["database_bytes"] > 0


def test_find_by_parent_id_through_the_facade(store: LineageStore) -> None:
    ingest = _ingest(store)
    with store.create_node(step_type="clean", parent=ingest) as clean:
        clean.add_meta("ok", True)
    assert store.find(parent_id=[]) == [ingest]
    assert [n.node_id for n in store.find(parent_id=[ingest.node_id])] == [
        clean.node_id
    ]


def test_export_writes_grepable_sidecars(store: LineageStore, tmp_path: Path) -> None:
    _ingest(store)
    node = store.latest(step_type="ingest")
    assert node is not None

    dest = store.export_metadata()
    assert dest == tmp_path / "proj" / "export"
    document = json.loads((dest / node.node_id / "meta.json").read_text())
    assert document["step_type"] == "ingest"
    assert document["healthy"] is True
    assert document["metadata"]["rows"]["value"] == 1
    assert "raw.csv" in document["artifacts"]
    assert set(document["provenance"]) >= {"user", "git_commit"}

    custom = store.export_metadata(dest=tmp_path / "sidecars")
    assert (custom / node.node_id / "meta.json").exists()


# ---------------------------------------------------------------------------
# Refusing roots this ancestree cannot open
# ---------------------------------------------------------------------------


def _legacy_root(tmp_path: Path) -> Path:
    """A 0.1.x store root: a config file at the top, a directory per node."""
    root = tmp_path / "old_project"
    (root / "ab12cd34").mkdir(parents=True)
    (root / ".lineage_config.json").write_text('{"rules": {}, "gen_triggers": []}')
    (root / "ab12cd34" / "meta.json").write_text('{"node_id": "ab12cd34"}')
    return root


def test_legacy_0_1_x_root_is_refused_not_reopened_empty(tmp_path: Path) -> None:
    """0.1.x kept nodes as directories, so there is no database to check a
    version stamp against. Without this guard the root looks fresh and the
    user is handed an empty store beside their old nodes."""
    root = _legacy_root(tmp_path)

    with pytest.raises(SchemaError, match="0.1.x store"):
        LineageStore(root)

    # Nothing created, nothing touched.
    assert not (root / "ancestree.db").exists()
    assert sorted(p.name for p in root.iterdir()) == [
        ".lineage_config.json",
        "ab12cd34",
    ]


def test_legacy_config_beside_a_real_store_still_opens(tmp_path: Path) -> None:
    """The guard keys on the database being absent. A 0.2.0 store whose root
    happens to carry the old config file is a working store, not a 0.1.x one."""
    root = _legacy_root(tmp_path)
    (root / ".lineage_config.json").unlink()
    LineageStore(root).close()
    (root / ".lineage_config.json").write_text("{}")

    store = LineageStore(root)
    assert store.find() == []
    store.close()
