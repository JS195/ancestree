"""Search/query correctness against the append-only journal + compaction index.

These tests deliberately target the seam introduced by journaling: every query
reads the in-memory cache, which is the on-disk snapshot with the journal
(`.index.log`) replayed on top. The risks that didn't exist with the old
"rewrite the whole snapshot every add" design are:

  * results that straddle a compaction boundary (some nodes live in
    `.index.json`, others only in `.index.log`),
  * deletions that exist only as `del` lines in the journal (the snapshot still
    lists the node),
  * a second instance that must observe another's adds *and* removes,
  * `get_most_recent` ordering when timestamps span both layers,
  * recovery when the journal is lost or torn (reconcile from disk).

To make the boundary deterministic rather than relying on the 128-entry floor,
most tests shrink `_COMPACT_MIN` so a handful of nodes already span snapshot and
journal, and assert that the split is genuinely present before querying.
"""

import json

import pytest

from ancestree import LineageStore
from tests.conftest import _make_node


@pytest.fixture
def split_store(bare_store):
    """A store tuned to compact aggressively, so even small workloads leave the
    index split across the snapshot and the journal."""
    bare_store.database._COMPACT_MIN = 3
    return bare_store


def _snapshot_ids(store):
    path = store.root / ".index.json"
    return set(json.loads(path.read_text())) if path.exists() else set()


def _log_records(store):
    path = store.root / ".index.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _assert_index_split(store):
    """Guard: prove the index really is spread across both layers, so the test
    below is exercising the boundary and not silently passing on a fully
    compacted index."""
    snap, log = _snapshot_ids(store), _log_records(store)
    assert snap, "expected some entries already compacted into the snapshot"
    assert log, "expected some entries still living only in the journal"


class TestSearchAcrossCompactionBoundary:
    def test_find_returns_nodes_from_both_layers(self, split_store):
        ids = {}
        for i in range(10):
            node = _make_node(split_store, "ingest", meta={"tag": "batch", "idx": i})
            ids[i] = node.node_id
        _assert_index_split(split_store)

        # Blanket search returns every node regardless of which layer holds it.
        found = {n.node_id for n in split_store.find_node(tag="batch")}
        assert found == set(ids.values())
        assert len(split_store.find_node()) == 10

        # A node that was compacted into the snapshot is still individually
        # addressable...
        first = split_store.find_node(idx=0)
        assert [n.node_id for n in first] == [ids[0]]
        # ...as is one that only exists in the journal.
        last = split_store.find_node(idx=9)
        assert [n.node_id for n in last] == [ids[9]]

    def test_found_nodes_hydrate_correct_metadata(self, split_store):
        for i in range(8):
            _make_node(
                split_store, "ingest", files=(f"f{i}.csv",), meta={"idx": i, "v": i * i}
            )
        _assert_index_split(split_store)

        for i in range(8):
            (node,) = split_store.find_node(idx=i)
            assert node.step_type == "ingest"
            assert node.metadata["v"]["value"] == i * i
            assert {p.name for p in node.artifacts()} == {f"f{i}.csv"}

    def test_predicate_and_multikey_match_across_layers(self, split_store):
        # 14 sits between compaction boundaries (3, 6, 12, 24...) so the index
        # genuinely straddles snapshot and journal.
        for i in range(14):
            _make_node(
                split_store,
                "ingest",
                meta={"idx": i, "even": i % 2 == 0, "group": "a" if i < 6 else "b"},
            )
        _assert_index_split(split_store)

        evens = sorted(
            n.metadata["idx"]["value"] for n in split_store.find_node(even=True)
        )
        assert evens == [0, 2, 4, 6, 8, 10, 12]

        big_evens = split_store.find_node(even=True, idx=lambda x: x >= 8)
        assert sorted(n.metadata["idx"]["value"] for n in big_evens) == [8, 10, 12]

        group_b = split_store.find_node(
            group="b", idx=lambda x: x is not None and x < 9
        )
        assert sorted(n.metadata["idx"]["value"] for n in group_b) == [6, 7, 8]


class TestDeletionsHonouredBySearch:
    """The 9998-node store: pruned nodes must vanish from every query even while
    the snapshot still lists them, because the journal carries the `del`."""

    def test_pruned_branch_disappears_from_search(self, split_store):
        root = _make_node(split_store, "ingest")
        clean = _make_node(split_store, "clean", parent=root)
        model = _make_node(split_store, "model", parent=clean)
        # Pad so a compaction lands with the chain present in the snapshot.
        for _ in range(6):
            _make_node(split_store, "ingest")

        assert len(split_store.find_node(step_type="clean")) == 1
        assert len(split_store.find_node(step_type="model")) == 1

        deleted = split_store.prune(clean, dry_run=False)
        assert {n.node_id for n in deleted} == {clean.node_id, model.node_id}

        # Live search reflects the deletion immediately.
        assert split_store.find_node(step_type="clean") == []
        assert split_store.find_node(step_type="model") == []
        assert [n.node_id for n in split_store.find_node(step_type="ingest")]
        assert split_store.get_node(model.node_id) is None
        assert split_store.get_child_nodes(root) == []

    def test_search_excludes_deletes_recorded_only_in_journal(self, split_store):
        a = _make_node(split_store, "ingest", meta={"keep": True})
        for _ in range(5):  # force a compaction so `a` lands in the snapshot
            _make_node(split_store, "ingest", meta={"keep": True})
        assert a.node_id in _snapshot_ids(split_store)

        split_store.prune(a, dry_run=False)

        # The deletion lives in the journal, not the snapshot...
        assert a.node_id in _snapshot_ids(split_store)
        assert {"_op": "del", "id": a.node_id} in _log_records(split_store)
        # ...yet no query surfaces it.
        assert a.node_id not in {n.node_id for n in split_store.find_node()}
        assert a.node_id not in {n.node_id for n in split_store.find_node(keep=True)}

    def test_deletion_survives_reopen_and_rebuild(self, split_store):
        keep = _make_node(split_store, "ingest", meta={"name": "keep"})
        drop = _make_node(split_store, "ingest", meta={"name": "drop"})
        for _ in range(4):
            _make_node(split_store, "ingest", meta={"name": "filler"})
        split_store.prune(drop, dry_run=False)

        # A brand-new instance replays the journal and agrees.
        reopened = LineageStore(root=split_store.root)
        assert reopened.find_node(name="drop") == []
        assert [n.node_id for n in reopened.find_node(name="keep")] == [keep.node_id]

        # And an explicit rebuild from disk (which ignores the journal entirely)
        # reaches the same answer.
        reopened.rebuild_db_from_disk()
        assert reopened.find_node(name="drop") == []
        assert [n.node_id for n in reopened.find_node(name="keep")] == [keep.node_id]


class TestMostRecentAcrossLayers:
    def test_most_recent_spans_snapshot_and_journal(self, split_store):
        last_id = None
        for i in range(9):
            last_id = _make_node(split_store, "ingest", meta={"idx": i}).node_id
        _assert_index_split(split_store)

        recent = split_store.get_most_recent_node()
        assert recent.node_id == last_id
        assert recent.metadata["idx"]["value"] == 8

    def test_most_recent_with_filter(self, split_store):
        ingest = _make_node(split_store, "ingest")
        first_clean = _make_node(split_store, "clean", parent=ingest)
        for _ in range(4):  # newer nodes of a different step_type in between
            _make_node(split_store, "ingest")
        last_clean = _make_node(split_store, "clean", parent=ingest)

        # The filter must pick the most recent 'clean', not the most recent node.
        assert (
            split_store.get_most_recent_node(step_type="clean").node_id
            == last_clean.node_id
        )
        assert first_clean.node_id != last_clean.node_id

    def test_most_recent_after_deleting_latest(self, split_store):
        nodes = [_make_node(split_store, "ingest", meta={"idx": i}) for i in range(6)]
        assert split_store.get_most_recent_node().node_id == nodes[-1].node_id

        split_store.prune(nodes[-1], dry_run=False)
        assert split_store.get_most_recent_node().node_id == nodes[-2].node_id


class TestLineageQueriesAcrossLayers:
    def test_lineage_and_find_in_lineage_span_layers(self, split_store):
        # A long single chain whose ancestors are scattered across both layers.
        ingest = _make_node(split_store, "ingest", meta={"depth": 0})
        # ingest -> clean -> model is the only legal 3-chain under the fixture
        # rules; pad with extra roots to push several compactions through.
        clean = _make_node(split_store, "clean", parent=ingest, meta={"depth": 1})
        model = _make_node(split_store, "model", parent=clean, meta={"depth": 2})
        for _ in range(6):  # padding to push compactions through
            _make_node(split_store, "ingest")
        _assert_index_split(split_store)

        lineage = split_store.get_lineage(model)
        assert [n.step_type for n in lineage] == ["ingest", "clean", "model"]
        assert [n.metadata["depth"]["value"] for n in lineage] == [0, 1, 2]

        cleans = split_store.find_in_lineage(model, step_type="clean")
        assert [n.node_id for n in cleans] == [clean.node_id]

        shallow = split_store.find_in_lineage(
            model, depth=lambda d: d is not None and d <= 1
        )
        assert sorted(n.metadata["depth"]["value"] for n in shallow) == [0, 1]

    def test_lineage_raises_for_pruned_ancestor(self, split_store):
        ingest = _make_node(split_store, "ingest")
        clean = _make_node(split_store, "clean", parent=ingest)
        model = _make_node(split_store, "model", parent=clean)
        for _ in range(4):
            _make_node(split_store, "ingest")

        # Prune the middle of the chain out from under the leaf.
        split_store.prune(clean, dry_run=False)
        # model was a descendant of clean, so it is gone too; its id is no
        # longer in the index, and lineage on it raises the documented KeyError.
        with pytest.raises(KeyError):
            split_store.get_lineage(model.node_id)


class TestCrossInstanceSearch:
    def test_reader_sees_writer_adds_and_prunes(self, tmp_path):
        writer = LineageStore(root=tmp_path / "shared", dedupe=False, chunk=False)
        writer.database._COMPACT_MIN = 3
        reader = LineageStore(root=tmp_path / "shared", dedupe=False, chunk=False)
        _ = reader.database.cache  # reader loads an empty index first

        made = [_make_node(writer, "ingest", meta={"tag": "x"}) for _ in range(7)]
        # Reader picks up every add via journal/snapshot mtime invalidation.
        assert {n.node_id for n in reader.find_node(tag="x")} == {
            n.node_id for n in made
        }

        writer.prune(made[0], dry_run=False)
        # Reader picks up the delete too.
        assert made[0].node_id not in {n.node_id for n in reader.find_node(tag="x")}
        assert len(reader.find_node(tag="x")) == 6


class TestRecoveryPaths:
    def test_search_recovers_when_journal_is_lost(self, split_store):
        ids = {
            _make_node(split_store, "ingest", meta={"tag": "t"}).node_id
            for _ in range(7)
        }
        # Some ids only exist in the journal at this point.
        journal_only = ids - _snapshot_ids(split_store)
        assert journal_only, "precondition: some ids live only in the journal"

        # Lose the journal entirely (simulating a crash before compaction).
        (split_store.root / ".index.log").unlink()

        # A fresh instance reconciles against the on-disk meta.json files, so
        # search still finds everything, including the journal-only nodes.
        reopened = LineageStore(root=split_store.root)
        found = {n.node_id for n in reopened.find_node(tag="t")}
        assert found == ids

    def test_search_tolerates_torn_journal_line(self, split_store):
        ids = {
            _make_node(split_store, "ingest", meta={"tag": "t"}).node_id
            for _ in range(7)
        }
        log = split_store.root / ".index.log"
        # Append a half-written line, as a crashed concurrent append might.
        with log.open("a") as f:
            f.write('{"id": "deadbeef", "meta": {"tag"')  # no newline, truncated

        reopened = LineageStore(root=split_store.root)
        found = {n.node_id for n in reopened.find_node(tag="t")}
        assert found == ids  # real nodes intact
        assert reopened.get_node("deadbeef") is None  # torn record ignored


class TestScaleGroundTruth:
    """Cross many real compactions (default 128 floor) and check every query
    against an independently-maintained ground-truth dict."""

    def test_counts_match_ground_truth_over_many_compactions(self, bare_store):
        truth = {}  # node_id -> bucket
        for i in range(400):
            bucket = ["red", "green", "blue"][i % 3]
            node = _make_node(bare_store, "ingest", meta={"bucket": bucket, "i": i})
            truth[node.node_id] = bucket

        # Multiple compactions must have occurred at this scale.
        assert len(_snapshot_ids(bare_store)) >= 256

        assert len(bare_store.find_node()) == 400
        for bucket in ("red", "green", "blue"):
            expected = {nid for nid, b in truth.items() if b == bucket}
            assert {n.node_id for n in bare_store.find_node(bucket=bucket)} == expected

        # Spot-check exact-value lookups land on the right unique node.
        for i in (0, 127, 128, 255, 256, 399):
            (node,) = bare_store.find_node(i=i)
            assert truth[node.node_id] == ["red", "green", "blue"][i % 3]
