"""Edge cases for the node creation lifecycle.

Pins the persistence contract of LineageStore.create_node:

1. A node persists on disk if and only if the user wrote something to it
   (an artifact file or a metadata entry).
2. Empty nodes never exist on disk — not during the context before the user
   acts, and not after it exits.
3. Nodes without a meta.json never exist on disk: every surviving node
   directory contains one, whatever path led to it.
4. If the user's code raises midway, anything written before the failure
   still persists — flagged unhealthy via the 'healthy' metadata field.

Run with: pytest tests/test_node_creation_edge_cases.py
"""

import json
from unittest import mock

import pytest

from ancestree import LineageStore


@pytest.fixture
def store(tmp_path):
    return LineageStore(root=tmp_path / "store", rules={"ingest": [None]})


def node_dirs(store):
    """Every node directory currently on disk (store internals excluded)."""
    return [d for d in store.root.iterdir() if d.is_dir()]


def assert_no_orphan_dirs(store):
    """Invariant: any node directory on disk must contain a meta.json."""
    for d in node_dirs(store):
        assert (d / "meta.json").exists(), (
            f"orphan node dir without meta.json: {d.name}"
        )


# ---------------------------------------------------------------------------
# 1. Persistence requires a write
# ---------------------------------------------------------------------------


class TestPersistenceRequiresWrite:
    def test_artifact_write_persists_node(self, store):
        with store.create_node(step_type="ingest") as node:
            (node / "data.csv").write_text("x")
        assert (store.root / node.node_id / "data.csv").exists()
        assert (store.root / node.node_id / "meta.json").exists()
        assert store.get_node(node.node_id) is not None

    def test_metadata_only_write_persists_node(self, store):
        with store.create_node(step_type="ingest") as node:
            node.add_meta("note", "metrics only")
        assert (store.root / node.node_id / "meta.json").exists()
        assert [n.node_id for n in store.find_node(note="metrics only")] == [
            node.node_id
        ]

    def test_deeply_nested_artifact_persists_node(self, store):
        with store.create_node(step_type="ingest") as node:
            (node / "a/b/c/deep.txt").write_text("x")
        assert (store.root / node.node_id / "a/b/c/deep.txt").exists()
        assert_no_orphan_dirs(store)

    def test_nothing_exists_before_first_write(self, store):
        with store.create_node(step_type="ingest") as node:
            assert not node.path.exists()
            (node / "data.csv").write_text("x")
            assert node.path.exists()


# ---------------------------------------------------------------------------
# 2. Empty nodes never exist
# ---------------------------------------------------------------------------


class TestEmptyNodesNeverExist:
    def test_untouched_node_leaves_no_trace(self, store):
        with mock.patch("warnings.warn") as mock_warn:
            with store.create_node(step_type="ingest") as node:
                pass
        assert not (store.root / node.node_id).exists()
        assert store.find_node(step_type="ingest") == []
        mock_warn.assert_called_once()

    def test_composed_path_without_write_is_discarded(self, store):
        # `node / ...` creates the directory eagerly; if the file is never
        # written the node is still empty and must not survive.
        with mock.patch("warnings.warn"):
            with store.create_node(step_type="ingest") as node:
                node / "never_written.csv"
        assert not (store.root / node.node_id).exists()
        assert node_dirs(store) == []

    def test_empty_file_counts_as_a_write(self, store):
        # A zero-byte artifact is still a deliberate user action.
        with store.create_node(step_type="ingest") as node:
            (node / "empty.txt").touch()
        assert (store.root / node.node_id / "meta.json").exists()

    def test_many_discarded_nodes_leave_store_clean(self, store):
        with mock.patch("warnings.warn"):
            for _ in range(5):
                with store.create_node(step_type="ingest"):
                    pass
        assert node_dirs(store) == []
        assert store.find_node(step_type="ingest") == []


# ---------------------------------------------------------------------------
# 3. Failure midway: prior work survives, flagged unhealthy
# ---------------------------------------------------------------------------


class TestFailureMidway:
    def test_artifact_written_before_failure_survives(self, store):
        with pytest.raises(RuntimeError, match="boom"):
            with store.create_node(step_type="ingest") as node:
                (node / "partial.csv").write_text("rows so far")
                raise RuntimeError("boom")

        assert (store.root / node.node_id / "partial.csv").exists()
        assert (store.root / node.node_id / "meta.json").exists()
        assert_no_orphan_dirs(store)

    def test_failed_node_is_marked_unhealthy(self, store):
        with pytest.raises(RuntimeError):
            with store.create_node(step_type="ingest") as node:
                node.add_meta("rows_processed", 42)
                raise RuntimeError("boom")

        retrieved = store.get_node(node.node_id)
        assert retrieved.metadata["healthy"]["value"] is False
        assert retrieved.metadata["rows_processed"]["value"] == 42

    def test_completed_node_is_marked_healthy(self, store):
        with store.create_node(step_type="ingest") as node:
            (node / "data.csv").write_text("x")

        retrieved = store.get_node(node.node_id)
        assert retrieved.metadata["healthy"]["value"] is True

    def test_unhealthy_nodes_are_searchable(self, store):
        with store.create_node(step_type="ingest") as good:
            (good / "ok.csv").write_text("x")
        with pytest.raises(RuntimeError):
            with store.create_node(step_type="ingest", parent=None) as bad:
                (bad / "broken.csv").write_text("x")
                raise RuntimeError("boom")

        assert {n.node_id for n in store.find_node(healthy=False)} == {bad.node_id}
        assert {n.node_id for n in store.find_node(healthy=True)} == {good.node_id}

    def test_failure_with_nothing_written_leaves_no_trace(self, store):
        with pytest.raises(RuntimeError):
            with store.create_node(step_type="ingest") as node:
                raise RuntimeError("boom")
        assert not (store.root / node.node_id).exists()
        assert store.find_node(step_type="ingest") == []

    def test_failure_after_composed_but_unwritten_path(self, store):
        # Directory exists (created by `node / ...`) but holds nothing:
        # still counts as untouched and must vanish.
        with pytest.raises(RuntimeError):
            with store.create_node(step_type="ingest") as node:
                node / "never_written.csv"
                raise RuntimeError("boom")
        assert not (store.root / node.node_id).exists()

    def test_keyboard_interrupt_is_not_swallowed_but_work_survives(self, store):
        # BaseException catches KeyboardInterrupt, so partial work is persisted
        # and flagged unhealthy before the interrupt propagates.
        with pytest.raises(KeyboardInterrupt):
            with store.create_node(step_type="ingest") as node:
                (node / "partial.csv").write_text("x")
                raise KeyboardInterrupt

        assert (store.root / node.node_id / "partial.csv").exists()
        assert (store.root / node.node_id / "meta.json").exists()

        found = store.find_node(step_type="ingest")
        assert len(found) == 1
        assert not found[0].metadata["healthy"]["value"]


# ---------------------------------------------------------------------------
# 4. Cross-cutting invariant: no orphan directories, ever
# ---------------------------------------------------------------------------


class TestNoOrphans:
    def test_mixed_workload_leaves_only_valid_nodes(self, store):
        survivors = set()

        with store.create_node(step_type="ingest") as n1:
            (n1 / "a.csv").write_text("x")
        survivors.add(n1.node_id)

        with mock.patch("warnings.warn"):
            with store.create_node(step_type="ingest"):
                pass  # empty -> discarded

        with pytest.raises(RuntimeError):
            with store.create_node(step_type="ingest") as n3:
                n3.add_meta("progress", 0.5)
                raise RuntimeError("boom")
        survivors.add(n3.node_id)  # touched before failure -> survives

        with pytest.raises(RuntimeError):
            with store.create_node(step_type="ingest"):
                raise RuntimeError("boom")  # untouched -> discarded

        assert {d.name for d in node_dirs(store)} == survivors
        assert_no_orphan_dirs(store)

        # Every survivor's meta.json is valid JSON with the health flag set.
        for d in node_dirs(store):
            meta = json.loads((d / "meta.json").read_text())
            assert meta["healthy"]["value"] in (True, False)

    def test_index_matches_disk_after_mixed_workload(self, store):
        with store.create_node(step_type="ingest") as kept:
            (kept / "a.csv").write_text("x")
        with mock.patch("warnings.warn"):
            with store.create_node(step_type="ingest"):
                pass

        assert set(store.database.cache) == {kept.node_id}
        store.rebuild_db_from_disk()
        assert set(store.database.cache) == {kept.node_id}
