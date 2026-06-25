"""Adversarial tests: a hostile or careless user actively trying to break
the store — sabotaged files, hijacked metadata, path tricks, concurrent
instances, and scale.

Genuine gaps found by these attacks are encoded as xfail(strict=True): they
document today's behaviour and fail loudly the day the gap is fixed, so the
marker can be removed.
"""

import shutil
import threading

import pytest

from ancestree import LineageStore
from tests.conftest import _make_node


class TestHostileMetadataValues:
    @pytest.fixture
    def node(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            yield node

    @pytest.mark.parametrize(
        "key", ["", " ", "key with spaces", "ключ", "🔥", "a.b[c]", "x" * 500]
    )
    def test_weird_keys_round_trip(self, bare_store, key):
        with bare_store.create_node(step_type="ingest") as node:
            node.add_meta(key, "v")
        reloaded = bare_store.get_node(node.node_id)
        assert reloaded.metadata[key]["value"] == "v"

    def test_html_injection_strings_round_trip_exactly(self, bare_store):
        payload = '<script>alert("pwned")</script><img src=x onerror=alert(1)>'
        with bare_store.create_node(step_type="ingest") as node:
            node.add_meta("note", payload)
        assert bare_store.get_node(node.node_id).metadata["note"]["value"] == payload

    def test_megabyte_value_persists(self, bare_store):
        big = "x" * 1_000_000
        with bare_store.create_node(step_type="ingest") as node:
            node.add_meta("blob", big)
        assert bare_store.get_node(node.node_id).metadata["blob"]["value"] == big

    def test_deeply_nested_json_round_trips(self, bare_store):
        value = {"a": [{"b": {"c": [1, None, True, {"d": "🎉"}]}}]}
        with bare_store.create_node(step_type="ingest") as node:
            node.add_meta("nested", value)
        assert bare_store.get_node(node.node_id).metadata["nested"]["value"] == value

    def test_empty_step_type_rejected(self, bare_store):
        # An empty step_type would silently bypass the rule check, so it is
        # rejected at create_node.
        for bad in ("", "   "):
            with pytest.raises(ValueError, match="non-empty"):
                with bare_store.create_node(step_type=bad):
                    pass

    def test_control_chars_and_overlong_step_type_rejected(self, bare_store):
        with pytest.raises(ValueError, match="printable"):
            with bare_store.create_node(step_type="bad\nstep"):
                pass
        with pytest.raises(ValueError, match="too long"):
            with bare_store.create_node(step_type="x" * 101):
                pass

    def test_ordinary_step_types_accepted(self, bare_store):
        # Normal labels, including non-Latin and symbols, still work.
        for ok in ("ingest", "train-model", "feature.v2", "clean data", "café", "步骤"):
            node = _make_node(bare_store, ok)
            assert bare_store.get_node(node.node_id).step_type == ok


class TestSystemKeyHijacking:
    def test_overwriting_only_system_keys_does_not_persist(self, bare_store):
        # Pinned: system-key overwrites don't count as "user touched the
        # node", so a node containing nothing else is still discarded.
        with pytest.warns(UserWarning, match="discarded"):
            with bare_store.create_node(step_type="ingest") as node:
                node.add_meta("node_id", "hijacked")
        assert not node.path.exists()

    def test_overwriting_reserved_key_is_rejected(self, bare_store):
        # Reserved structural keys cannot be hijacked: add_meta refuses them,
        # so a user can't corrupt their own lineage or the health flags the
        # store relies on.
        with bare_store.create_node(step_type="ingest") as node:
            (node / "x.txt").write_text("x")
            with pytest.raises(ValueError, match="reserved key"):
                node.add_meta("parent_id", "zzzzzzzz")


class TestPathTricks:
    def test_writes_outside_node_directory_are_refused(self, bare_store):
        # __truediv__ refuses any path that escapes the node directory, so the
        # escaping write never happens and nothing lands in the store root. A
        # path that stays inside the node is still fine.
        with bare_store.create_node(step_type="ingest") as node:
            (node / "legit.txt").write_text("ok")
            with pytest.raises(ValueError, match="escapes the node"):
                _ = node / "../escaped.txt"
        assert not (bare_store.root / "escaped.txt").exists()
        names = {p.name for p in bare_store.get_node(node.node_id).artifacts()}
        assert names == {"legit.txt"}

    @pytest.mark.xfail(
        strict=True, reason="get_node on a path to a file raises NotADirectoryError"
    )
    def test_get_node_on_file_path_returns_none(self, chain_store):
        store, nodes = chain_store
        assert store.get_node(f"{nodes['ingest'].node_id}/data.csv") is None

    def test_stray_files_in_store_root_are_ignored(self, bare_store, make_node):
        (bare_store.root / "README.txt").write_text("not a node")
        node = make_node(bare_store, "ingest")
        reopened = LineageStore(root=bare_store.root)
        assert [n.node_id for n in reopened.find_node()] == [node.node_id]


class TestFilesystemSabotage:
    def test_corrupt_snapshot_recovers_via_rebuild(self, chain_store):
        store, nodes = chain_store
        (store.root / ".index.json").write_text("{ this is not json")
        reopened = LineageStore(root=store.root)
        reopened.rebuild_db_from_disk()
        assert len(reopened.find_node()) == len(nodes)

    @pytest.mark.xfail(
        strict=True, reason="corrupt .index.json is not yet self-healed on load"
    )
    def test_corrupt_snapshot_self_heals_on_query(self, chain_store):
        store, nodes = chain_store
        (store.root / ".index.json").write_text("{ this is not json")
        reopened = LineageStore(root=store.root)
        assert len(reopened.find_node()) == len(nodes)

    def test_corrupt_meta_of_indexed_node_degrades_gracefully(self, chain_store):
        store, nodes = chain_store
        (nodes["clean"].path / "meta.json").write_text("{ garbage")
        # Searches still answer from the index; direct load returns None.
        reopened = LineageStore(root=store.root)
        assert len(reopened.find_node(step_type="clean")) == 1
        assert reopened.get_node(nodes["clean"].node_id) is None

    @pytest.mark.xfail(
        strict=True,
        reason="rebuild_from_disk crashes on a corrupt meta.json instead of skipping it",
    )
    def test_rebuild_skips_corrupt_meta(self, chain_store):
        store, nodes = chain_store
        (nodes["clean"].path / "meta.json").write_text("{ garbage")
        reopened = LineageStore(root=store.root)
        reopened.rebuild_db_from_disk()
        assert len(reopened.find_node()) == len(nodes) - 1

    def test_manually_deleted_node_disappears_on_reload(self, chain_store):
        store, nodes = chain_store
        shutil.rmtree(nodes["model"].path)
        reopened = LineageStore(root=store.root)
        assert reopened.find_node(step_type="model") == []

    def test_cycle_in_lineage_is_detected(self, chain_store):
        store, nodes = chain_store
        a, b = nodes["ingest"].node_id, nodes["clean"].node_id
        store.database.cache[a]["parent_id"] = [b]  # a <-> b
        with pytest.raises(ValueError, match="Cycle detected"):
            store.get_lineage(b)


class TestConcurrentInstances:
    def test_reader_sees_other_writers_nodes(self, tmp_path):
        writer = LineageStore(root=tmp_path / "shared")
        reader = LineageStore(root=tmp_path / "shared")
        _ = reader.database.cache  # reader loads before the write
        node = _make_node(writer, "ingest", meta={"tag": "fresh"})
        assert [n.node_id for n in reader.find_node(tag="fresh")] == [node.node_id]

    def test_interleaved_writers_lose_nothing(self, tmp_path):
        a = LineageStore(root=tmp_path / "shared")
        b = LineageStore(root=tmp_path / "shared")
        made = {_make_node(s, "ingest").node_id for s in (a, b, a, b)}
        for store in (a, b):
            assert {n.node_id for n in store.find_node(step_type="ingest")} == made
        # The on-disk index (snapshot + journal) is durable and complete: a
        # brand-new instance reconstructs every node without touching meta.json.
        fresh = LineageStore(root=tmp_path / "shared")
        assert set(fresh.database.cache) == made

    def test_prune_in_one_instance_visible_in_other(self, tmp_path):
        a = LineageStore(root=tmp_path / "shared")
        b = LineageStore(root=tmp_path / "shared")
        node = _make_node(a, "ingest")
        assert b.find_node(step_type="ingest") != []
        a.prune(node, dry_run=False)
        assert b.find_node(step_type="ingest") == []

    def test_parallel_writers_threads(self, tmp_path):
        # Each thread uses its own store instance (documented model).
        root = tmp_path / "shared"
        errors = []

        def work(i):
            try:
                store = LineageStore(root=root)
                with store.create_node(step_type="ingest") as node:
                    (node / "out.txt").write_text(str(i))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []
        # Disk is the source of truth: every node must exist and be indexed
        fresh = LineageStore(root=root)
        assert len(fresh.find_node(step_type="ingest")) == 8


class TestPredicateAbuse:
    def test_raising_predicate_means_no_match(self, chain_store):
        store, _ = chain_store
        # accuracy is missing on most nodes: None > 0.5 raises TypeError,
        # which is swallowed as "no match" rather than crashing the search
        found = store.find_node(accuracy=lambda a: a > 0.5)
        assert [n.step_type for n in found] == ["model"]

    def test_always_true_predicate_matches_missing_keys(self, chain_store):
        # Pinned quirk: the predicate receives None for absent keys, so a
        # blanket-true predicate matches every node.
        store, nodes = chain_store
        assert len(store.find_node(banana=lambda v: True)) == len(nodes)

    def test_searching_missing_key_for_none_matches_all(self, chain_store):
        # Pinned quirk: absent keys read as None, so value=None matches
        # every node rather than none of them.
        store, nodes = chain_store
        assert len(store.find_node(banana=None)) == len(nodes)


class TestScale:
    def test_long_chain_lineage_and_prune(self, tmp_path):
        store = LineageStore(root=tmp_path / "deep")
        parent = None
        for i in range(60):
            with store.create_node(step_type="step", parent=parent) as node:
                node.add_meta("i", i)
            parent = node
        lineage = store.get_lineage(parent)
        assert [n.metadata["i"]["value"] for n in lineage] == list(range(60))
        deleted = store.prune(lineage[0], dry_run=False)
        assert len(deleted) == 60
        assert store.find_node() == []

    def test_wide_store_search_and_recency(self, tmp_path):
        store = LineageStore(root=tmp_path / "wide")
        last = None
        for i in range(150):
            with store.create_node(step_type="run") as node:
                node.add_meta("i", i)
            last = node
        assert len(store.find_node(step_type="run")) == 150
        assert store.get_most_recent_node(step_type="run").node_id == last.node_id
        assert len(store.find_node(i=lambda v: v is not None and v % 2 == 0)) == 75


class TestWeirdEnvironments:
    def test_unicode_store_root(self, tmp_path):
        store = LineageStore(root=tmp_path / "données ünïcode 🔬")
        node = _make_node(store, "ingest", meta={"étiquette": "café"})
        reopened = LineageStore(root=store.root)
        assert reopened.get_node(node.node_id).metadata["étiquette"]["value"] == "café"
        reopened.generate_web_graph()
        assert (store.root / "interactive_pipeline.html").exists()

    def test_nested_root_is_created(self, tmp_path):
        store = LineageStore(root=tmp_path / "a" / "b" / "c")
        _make_node(store, "ingest")
        assert (tmp_path / "a" / "b" / "c" / ".lineage_config.json").exists()
