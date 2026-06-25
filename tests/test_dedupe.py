"""Content-addressed deduplication (LineageStore(dedupe=True)).

When dedupe is enabled, a node that is content-identical to one already in the
store is not created a second time: create_node reuses the existing node and
rebinds the `as node` variable onto it. "Content-identical" means same
step_type, same parent, same user metadata, and byte-identical artifacts —
volatile fields (node_id, timestamp, duration, size) and provenance are ignored.

Run with: pytest tests/test_dedupe.py
"""

import time

import pytest

from ancestree import LineageStore


def node_dirs(store):
    """Every node directory currently on disk (store internals excluded, e.g.
    the .chunks pool)."""
    return [
        d for d in store.root.iterdir() if d.is_dir() and not d.name.startswith(".")
    ]


@pytest.fixture
def dedupe_store(tmp_path):
    """A store with deduplication enabled and no lineage rules. Chunking is off
    so these tests isolate whole-node dedup; the two features are exercised
    together in test_chunking.py::test_dedupe_and_chunk_together."""
    return LineageStore(root=tmp_path / "store", dedupe=True, chunk=False)


def _node(store, step_type, parent=None, files=(("data.csv", "x"),), meta=None):
    """Create a node with the given (name, content) files and metadata."""
    with store.create_node(step_type=step_type, parent=parent) as node:
        for fname, content in files:
            (node / fname).write_text(content)
        for key, value in (meta or {}).items():
            node.add_meta(key, value)
    time.sleep(0.002)  # force distinct timestamps so only content can match
    return node


# ---------------------------------------------------------------------------
# Identical content is reused
# ---------------------------------------------------------------------------


class TestReuse:
    def test_identical_nodes_reuse_single_directory(self, dedupe_store):
        first = _node(dedupe_store, "ingest", meta={"rows": 10})
        second = _node(dedupe_store, "ingest", meta={"rows": 10})

        assert second.node_id == first.node_id
        assert len(node_dirs(dedupe_store)) == 1

    def test_yielded_variable_is_rebound_onto_existing(self, dedupe_store):
        first = _node(dedupe_store, "ingest")

        with dedupe_store.create_node(step_type="ingest") as node:
            (node / "data.csv").write_text("x")
        # After the block the variable points at the pre-existing node.
        assert node.node_id == first.node_id
        assert node.path == first.path

    def test_reused_node_is_usable_as_parent(self, dedupe_store):
        first = _node(dedupe_store, "ingest")

        with dedupe_store.create_node(step_type="ingest") as dup:
            (dup / "data.csv").write_text("x")
        child = _node(dedupe_store, "clean", parent=dup, files=(("c.csv", "y"),))

        assert child.parent_id == first.node_id
        assert dedupe_store.get_lineage(child)[0].node_id == first.node_id

    def test_timestamp_and_provenance_do_not_block_reuse(self, dedupe_store):
        # Two runs separated in time: timestamps differ, content does not.
        first = _node(dedupe_store, "ingest", meta={"rows": 10})
        time.sleep(0.01)
        second = _node(dedupe_store, "ingest", meta={"rows": 10})
        assert second.node_id == first.node_id


# ---------------------------------------------------------------------------
# Different content stays distinct
# ---------------------------------------------------------------------------


class TestDistinct:
    def test_different_artifact_bytes_are_distinct(self, dedupe_store):
        first = _node(dedupe_store, "ingest", files=(("data.csv", "aaa"),))
        second = _node(dedupe_store, "ingest", files=(("data.csv", "bbb"),))
        assert second.node_id != first.node_id
        assert len(node_dirs(dedupe_store)) == 2

    def test_different_metadata_is_distinct(self, dedupe_store):
        first = _node(dedupe_store, "ingest", meta={"rows": 10})
        second = _node(dedupe_store, "ingest", meta={"rows": 20})
        assert second.node_id != first.node_id

    def test_same_content_different_parent_is_distinct(self, dedupe_store):
        root_a = _node(dedupe_store, "ingest", files=(("a.csv", "a"),))
        root_b = _node(dedupe_store, "ingest", files=(("b.csv", "b"),))
        # Identical clean content, but hung off different parents.
        clean_a = _node(dedupe_store, "clean", parent=root_a, files=(("c.csv", "c"),))
        clean_b = _node(dedupe_store, "clean", parent=root_b, files=(("c.csv", "c"),))
        assert clean_a.node_id != clean_b.node_id

    def test_different_filename_same_bytes_is_distinct(self, dedupe_store):
        first = _node(dedupe_store, "ingest", files=(("a.csv", "x"),))
        second = _node(dedupe_store, "ingest", files=(("b.csv", "x"),))
        assert second.node_id != first.node_id


# ---------------------------------------------------------------------------
# Opt-in: off by default
# ---------------------------------------------------------------------------


class TestOptIn:
    def test_duplicates_kept_when_dedupe_disabled(self, tmp_path):
        store = LineageStore(root=tmp_path / "store", dedupe=False, chunk=False)
        first = _node(store, "ingest", meta={"rows": 10})
        second = _node(store, "ingest", meta={"rows": 10})
        assert second.node_id != first.node_id
        assert len(node_dirs(store)) == 2

    def test_dedupe_is_a_per_instance_flag_not_persisted_in_config(self, tmp_path):
        # The flag lives on the instance, never in the on-disk config: opening
        # with a non-default value must not leak into a later plain open.
        root = tmp_path / "store"
        explicit = LineageStore(root=root, dedupe=False)
        assert explicit.dedupe is False
        reopened = LineageStore(root=root)  # dedupe not resupplied -> the default
        assert reopened.dedupe is True


# ---------------------------------------------------------------------------
# Whole-pipeline reuse and persistence across reopen
# ---------------------------------------------------------------------------


class TestPipeline:
    def test_rerunning_identical_chain_reuses_every_node(self, dedupe_store):
        def build():
            ingest = _node(dedupe_store, "ingest", files=(("raw.csv", "r"),))
            clean = _node(
                dedupe_store, "clean", parent=ingest, files=(("clean.csv", "c"),)
            )
            model = _node(
                dedupe_store, "model", parent=clean, files=(("m.pkl", "m"),)
            )
            return ingest, clean, model

        first = build()
        second = build()

        assert [n.node_id for n in first] == [n.node_id for n in second]
        assert len(node_dirs(dedupe_store)) == 3

    def test_dedupe_works_after_store_reopen(self, tmp_path):
        root = tmp_path / "store"
        store = LineageStore(root=root, dedupe=True)
        first = _node(store, "ingest", meta={"rows": 10})

        # New store instance: the hash index must rebuild from the on-disk
        # index rather than relying on in-memory state.
        reopened = LineageStore(root=root, dedupe=True)
        second = _node(reopened, "ingest", meta={"rows": 10})

        assert second.node_id == first.node_id
        assert len(node_dirs(reopened)) == 1


# ---------------------------------------------------------------------------
# Interaction with other lifecycle rules
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_content_hash_key_is_reserved(self, dedupe_store):
        with dedupe_store.create_node(step_type="ingest") as node:
            node.add_meta("rows", 1)
            with pytest.raises(ValueError, match="reserved"):
                node.add_meta("content_hash", "deadbeef")

    def test_failed_run_is_not_deduplicated(self, dedupe_store):
        first = _node(dedupe_store, "ingest", meta={"rows": 10})

        # A run that raises after writing identical content must persist as its
        # own (unhealthy) node, never merge into the healthy one.
        with pytest.raises(RuntimeError):
            with dedupe_store.create_node(step_type="ingest") as node:
                node.add_meta("rows", 10)
                (node / "data.csv").write_text("x")
                raise RuntimeError("boom")

        assert node.node_id != first.node_id
        assert node.metadata["healthy"]["value"] is False
        assert len(node_dirs(dedupe_store)) == 2
