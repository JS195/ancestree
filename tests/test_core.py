"""Tests for ancestree.core.LineageStore."""
import json
import shelve
import shutil
from unittest import mock

import pytest

from ancestree import LineageStore
from ancestree.models import Node
from tests.conftest import RULES, TRIGGERS


# ---------------------------------------------------------------------------
# Initialisation / config persistence
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_root_and_config(self, tmp_path):
        store = LineageStore(root=tmp_path / "store", rules=RULES, gen_triggers=TRIGGERS)
        assert store.root.is_dir()
        config = json.loads((store.root / ".lineage_config.json").read_text())
        assert config["rules"] == RULES
        assert config["triggers"] == TRIGGERS

    def test_accepts_str_root(self, tmp_path):
        store = LineageStore(root=str(tmp_path / "store"), rules=RULES)
        assert store.root == tmp_path / "store"

    def test_no_rules_defaults_to_empty(self, tmp_path):
        store = LineageStore(root=tmp_path / "store")
        assert store.rules == {}
        assert store.triggers == []

    def test_config_persists_across_reinit(self, tmp_path):
        LineageStore(root=tmp_path / "store", rules=RULES, gen_triggers=TRIGGERS)
        reloaded = LineageStore(root=tmp_path / "store")
        assert reloaded.rules == RULES
        assert reloaded.triggers == TRIGGERS

    def test_config_is_immutable_after_creation(self, tmp_path):
        LineageStore(root=tmp_path / "store", rules=RULES)
        changed = LineageStore(root=tmp_path / "store", rules={"other": [None]})
        assert changed.rules == RULES


# ---------------------------------------------------------------------------
# create_node
# ---------------------------------------------------------------------------

class TestCreateNode:
    def test_persists_node_with_artifacts(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            (node / "data.csv").write_text("id,val\n1,100")
            node_id = node.node_id

        assert (bare_store.root / node_id / "data.csv").exists()
        assert (bare_store.root / node_id / "meta.json").exists()
        retrieved = bare_store.get_node(node_id)
        assert retrieved is not None
        assert retrieved.step_type == "ingest"
        assert retrieved.parent_id is None
        assert retrieved.generation == 0

    def test_persists_metadata_only_node(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            node.add_meta("note", "metrics only")
            node_id = node.node_id

        assert (bare_store.root / node_id / "meta.json").exists()
        assert [n.node_id for n in bare_store.find_node(note="metrics only")] == [node_id]

    def test_empty_node_is_discarded_with_warning(self, bare_store):
        with mock.patch("warnings.warn") as mock_warn:
            with bare_store.create_node(step_type="ingest") as node:
                node_id = node.node_id

        assert not (bare_store.root / node_id).exists()
        assert bare_store.find_node(step_type="ingest") == []
        mock_warn.assert_called_once()
        message = mock_warn.call_args.args[0]
        assert node_id in message
        assert "discarded" in message

    def test_exception_rolls_back_node(self, bare_store):
        with pytest.raises(RuntimeError, match="boom"):
            with bare_store.create_node(step_type="ingest") as node:
                (node / "data.csv").write_text("partial")
                node_id = node.node_id
                raise RuntimeError("boom")

        assert not (bare_store.root / node_id).exists()
        assert bare_store.find_node(step_type="ingest") == []

    def test_explicit_parent_as_node_object(self, bare_store, make_node):
        ingest = make_node(bare_store, "ingest")
        clean = make_node(bare_store, "clean", parent=ingest)
        assert clean.parent_id == ingest.node_id

    def test_explicit_parent_as_id_string(self, bare_store, make_node):
        ingest = make_node(bare_store, "ingest")
        clean = make_node(bare_store, "clean", parent=ingest.node_id)
        assert clean.parent_id == ingest.node_id

    def test_auto_parents_to_most_recent_allowed(self, bare_store, make_node):
        make_node(bare_store, "ingest")
        newer = make_node(bare_store, "ingest", parent=None)
        clean = make_node(bare_store, "clean")  # no parent supplied
        assert clean.parent_id == newer.node_id

    def test_invalid_transition_raises_value_error(self, bare_store):
        # "model" requires a "clean" parent; none exists, so parent resolves
        # to None which is not an allowed parent type.
        with pytest.raises(ValueError, match="Invalid transition"):
            with bare_store.create_node(step_type="model"):
                pass

    def test_invalid_explicit_parent_raises(self, bare_store, make_node):
        ingest = make_node(bare_store, "ingest")
        with pytest.raises(ValueError, match="Invalid transition"):
            with bare_store.create_node(step_type="model", parent=ingest):
                pass

    def test_unruled_step_type_is_permitted(self, bare_store, make_node):
        # Step types absent from the rules dict bypass transition checks.
        report = make_node(bare_store, "report")
        assert report.step_type == "report"
        assert report.parent_id is None

    def test_generation_increments_on_trigger(self, chain_store):
        store, nodes = chain_store
        # TRIGGERS = ["clean"]: ingest stays 0, clean bumps to 1, model inherits 1.
        assert nodes["ingest"].generation == 0
        assert nodes["clean"].generation == 1
        assert nodes["model"].generation == 1

    def test_trigger_on_root_node_keeps_generation_zero(self, tmp_path, make_node):
        store = LineageStore(
            root=tmp_path / "s", rules={"ingest": [None]}, gen_triggers=["ingest"]
        )
        node = make_node(store, "ingest")
        assert node.generation == 0


# ---------------------------------------------------------------------------
# get_node
# ---------------------------------------------------------------------------

class TestGetNode:
    def test_resolves_id_string(self, chain_store):
        store, nodes = chain_store
        found = store.get_node(nodes["clean"].node_id)
        assert found.node_id == nodes["clean"].node_id
        assert found.step_type == "clean"
        assert found.parent_id == nodes["ingest"].node_id

    def test_passes_through_node_instance(self, chain_store):
        store, nodes = chain_store
        assert store.get_node(nodes["clean"]) is nodes["clean"]

    def test_none_returns_none(self, bare_store):
        assert bare_store.get_node(None) is None

    def test_empty_string_returns_none(self, bare_store):
        assert bare_store.get_node("") is None

    def test_none_like_string_returns_none(self, bare_store):
        assert bare_store.get_node("None") is None
        assert bare_store.get_node("none") is None

    def test_unknown_id_returns_none(self, bare_store):
        assert bare_store.get_node("deadbeef") is None

    def test_directory_without_meta_json_returns_none(self, bare_store):
        (bare_store.root / "straydir").mkdir()
        assert bare_store.get_node("straydir") is None

    def test_malformed_meta_json_returns_none(self, bare_store):
        bad = bare_store.root / "badnode1"
        bad.mkdir()
        (bad / "meta.json").write_text("{not valid json")
        assert bare_store.get_node("badnode1") is None

    def test_flat_schema_meta_json_returns_none(self, bare_store):
        # Valid JSON, but values are not the nested {value: ...} entries.
        bad = bare_store.root / "badnode2"
        bad.mkdir()
        (bad / "meta.json").write_text(json.dumps({"generation": 1, "parent_id": None}))
        assert bare_store.get_node("badnode2") is None


# ---------------------------------------------------------------------------
# find_node
# ---------------------------------------------------------------------------

class TestFindNode:
    def test_empty_store_returns_empty_list(self, bare_store):
        assert bare_store.find_node(step_type="ingest") == []

    def test_no_match_returns_empty_list(self, chain_store):
        store, _ = chain_store
        assert store.find_node(step_type="nonexistent") == []

    def test_finds_by_step_type(self, branch_store):
        store, nodes = branch_store
        found = {n.node_id for n in store.find_node(step_type="clean")}
        assert found == {nodes["left"].node_id, nodes["right"].node_id}

    def test_multiple_criteria_are_anded(self, branch_store):
        store, nodes = branch_store
        found = store.find_node(step_type="clean", parent_id=nodes["root"].node_id)
        assert len(found) == 2
        found = store.find_node(step_type="clean", parent_id="bogus")
        assert found == []

    def test_finds_by_user_metadata(self, chain_store):
        store, nodes = chain_store
        found = store.find_node(source="api")
        assert [n.node_id for n in found] == [nodes["ingest"].node_id]

    def test_callable_matcher(self, chain_store):
        store, nodes = chain_store
        found = store.find_node(accuracy=lambda a: a is not None and a > 0.8)
        assert [n.node_id for n in found] == [nodes["model"].node_id]

    def test_stale_index_yields_none_entries(self, chain_store):
        # Pins current behaviour: if a node directory is deleted out-of-band
        # while its index entry remains, find_node returns None placeholders.
        store, nodes = chain_store
        shutil.rmtree(store.root / nodes["model"].node_id)
        found = store.find_node(step_type="model")
        assert found == [None]


# ---------------------------------------------------------------------------
# get_lineage / find_in_lineage
# ---------------------------------------------------------------------------

class TestLineage:
    def test_linear_chain_oldest_first(self, chain_store):
        store, nodes = chain_store
        lineage = store.get_lineage(nodes["model"].node_id)
        assert [n.node_id for n in lineage] == [
            nodes["ingest"].node_id,
            nodes["clean"].node_id,
            nodes["model"].node_id,
        ]

    def test_accepts_node_instance(self, chain_store):
        store, nodes = chain_store
        lineage = store.get_lineage(nodes["clean"])
        assert [n.step_type for n in lineage] == ["ingest", "clean"]

    def test_root_node_lineage_is_itself(self, chain_store):
        store, nodes = chain_store
        lineage = store.get_lineage(nodes["ingest"].node_id)
        assert [n.node_id for n in lineage] == [nodes["ingest"].node_id]

    def test_unknown_id_raises_key_error(self, chain_store):
        store, _ = chain_store
        with pytest.raises(KeyError, match="not found in the index"):
            store.get_lineage("deadbeef")

    def test_orphan_after_nonrecursive_prune_raises(self, branch_store):
        store, nodes = branch_store
        store.prune(nodes["left"], recursive=False, dry_run=False)
        with pytest.raises(KeyError, match="not found in the index"):
            store.get_lineage(nodes["leaf"].node_id)

    def test_find_in_lineage_by_step_type(self, chain_store):
        store, nodes = chain_store
        matches = store.find_in_lineage(nodes["model"].node_id, step_type="clean")
        assert [n.node_id for n in matches] == [nodes["clean"].node_id]

    def test_find_in_lineage_excludes_other_branches(self, branch_store):
        store, nodes = branch_store
        matches = store.find_in_lineage(nodes["leaf"].node_id, step_type="clean")
        assert [n.node_id for n in matches] == [nodes["left"].node_id]

    def test_find_in_lineage_callable(self, chain_store):
        store, nodes = chain_store
        matches = store.find_in_lineage(
            nodes["model"].node_id, accuracy=lambda a: a is not None and a > 0.8
        )
        assert [n.node_id for n in matches] == [nodes["model"].node_id]

    def test_find_in_lineage_no_match(self, chain_store):
        store, nodes = chain_store
        assert store.find_in_lineage(nodes["model"].node_id, step_type="zzz") == []


# ---------------------------------------------------------------------------
# get_most_recent_node / from_parent / get_child_nodes
# ---------------------------------------------------------------------------

class TestQueries:
    def test_most_recent_node_overall(self, chain_store):
        store, nodes = chain_store
        assert store.get_most_recent_node().node_id == nodes["model"].node_id

    def test_most_recent_node_filtered(self, branch_store):
        store, nodes = branch_store
        most_recent = store.get_most_recent_node(step_type="clean")
        assert most_recent.node_id == nodes["right"].node_id

    def test_most_recent_node_no_match_returns_none(self, chain_store):
        store, _ = chain_store
        assert store.get_most_recent_node(step_type="zzz") is None

    def test_from_parent_returns_matching_artifacts(self, chain_store):
        store, nodes = chain_store
        paths = store.from_parent(nodes["clean"].node_id, "data.csv")
        assert len(paths) == 1
        assert paths[0].name == "data.csv"
        assert paths[0].parts[0] == nodes["ingest"].node_id

    def test_from_parent_on_root_returns_empty(self, chain_store):
        store, nodes = chain_store
        assert store.from_parent(nodes["ingest"].node_id, "data.csv") == []

    def test_from_parent_no_matching_file(self, chain_store):
        store, nodes = chain_store
        assert store.from_parent(nodes["clean"].node_id, "missing.bin") == []

    def test_get_child_nodes(self, branch_store):
        store, nodes = branch_store
        children = {n.node_id for n in store.get_child_nodes(nodes["root"])}
        assert children == {nodes["left"].node_id, nodes["right"].node_id}

    def test_get_child_nodes_leaf_is_empty(self, branch_store):
        store, nodes = branch_store
        assert store.get_child_nodes(nodes["leaf"]) == []

    def test_get_child_nodes_unknown_node(self, bare_store):
        assert bare_store.get_child_nodes("deadbeef") == []


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

class TestPrune:
    def test_dry_run_default_deletes_nothing(self, branch_store, capsys):
        store, nodes = branch_store
        store.prune(nodes["root"])
        out = capsys.readouterr().out
        assert "Would delete" in out
        for node in nodes.values():
            assert (store.root / node.node_id).exists()
        assert len(store.find_node(step_type="clean")) == 2

    def test_dry_run_lists_whole_branch(self, branch_store, capsys):
        store, nodes = branch_store
        store.prune(nodes["root"], recursive=True)
        out = capsys.readouterr().out
        for node in nodes.values():
            assert node.node_id in out

    def test_recursive_prune_removes_branch(self, branch_store):
        store, nodes = branch_store
        store.prune(nodes["left"], recursive=True, dry_run=False)
        assert not (store.root / nodes["left"].node_id).exists()
        assert not (store.root / nodes["leaf"].node_id).exists()
        # Siblings and ancestors untouched.
        assert (store.root / nodes["root"].node_id).exists()
        assert (store.root / nodes["right"].node_id).exists()
        remaining = {n.node_id for n in store.find_node(step_type="clean")}
        assert remaining == {nodes["right"].node_id}

    def test_nonrecursive_prune_removes_single_node(self, branch_store):
        store, nodes = branch_store
        store.prune(nodes["left"], recursive=False, dry_run=False)
        assert not (store.root / nodes["left"].node_id).exists()
        assert (store.root / nodes["leaf"].node_id).exists()

    def test_prune_unknown_id_is_noop(self, bare_store):
        assert bare_store.prune("deadbeef", dry_run=False) is None

    def test_prune_root_directory_is_forbidden(self, bare_store):
        root_as_node = Node(bare_store.root, "fake", 0, None, "x")
        with pytest.raises(PermissionError, match="Cannot prune the root"):
            bare_store.prune(root_as_node, dry_run=False)


# ---------------------------------------------------------------------------
# rebuild_db_from_disk
# ---------------------------------------------------------------------------

class TestRebuild:
    @staticmethod
    def _shelf_contents(store):
        with shelve.open(store.database.shelf_path) as db:
            return {key: db[key] for key in db.keys()}

    @staticmethod
    def _delete_shelf(store):
        for path in store.root.glob("metadata_shelf*"):
            path.unlink()

    def test_rebuilt_shelf_matches_incrementally_built_one(self, chain_store):
        store, nodes = chain_store
        before = self._shelf_contents(store)

        self._delete_shelf(store)
        store.rebuild_db_from_disk()

        assert self._shelf_contents(store) == before

    def test_queries_behave_identically_after_rebuild(self, branch_store):
        store, nodes = branch_store
        lineage_before = [n.node_id for n in store.get_lineage(nodes["leaf"].node_id)]
        find_before = {n.node_id for n in store.find_node(step_type="clean")}
        recent_before = store.get_most_recent_node(step_type="clean").node_id

        self._delete_shelf(store)
        store.rebuild_db_from_disk()

        assert [n.node_id for n in store.get_lineage(nodes["leaf"].node_id)] == lineage_before
        assert {n.node_id for n in store.find_node(step_type="clean")} == find_before
        assert store.get_most_recent_node(step_type="clean").node_id == recent_before

    def test_rebuild_skips_directories_without_meta(self, chain_store):
        store, nodes = chain_store
        (store.root / "strayfolder").mkdir()
        self._delete_shelf(store)
        store.rebuild_db_from_disk()
        assert "strayfolder" not in self._shelf_contents(store)
        assert len(self._shelf_contents(store)) == len(nodes)


# ---------------------------------------------------------------------------
# generate_web_graph (thin wrapper; full coverage in test_vis.py)
# ---------------------------------------------------------------------------

class TestWebGraph:
    def test_generates_html_in_store_root(self, chain_store, capsys):
        store, _ = chain_store
        store.generate_web_graph()
        out = capsys.readouterr().out
        assert "Graph generated at" in out
        assert (store.root / "interactive_pipeline.html").exists()
