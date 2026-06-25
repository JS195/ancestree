"""Functional coverage of the LineageStore public API.

Complements test_node_creation_edge_cases.py (persistence semantics) with
the store-level behaviours: rules, config persistence, generations, search,
lineage, pruning, automatic capture, and data_type inference.
"""

import time
from pathlib import Path

import pytest

from ancestree import LineageStore
from tests.conftest import RULES, TRIGGERS


class TestConfigPersistence:
    def test_rules_survive_reinstantiation(self, bare_store, make_node):
        make_node(bare_store, "ingest")
        reopened = LineageStore(root=bare_store.root)  # no rules supplied
        assert reopened.rules == RULES
        assert reopened.triggers == TRIGGERS

    def test_first_config_wins_over_later_rules(self, bare_store):
        conflicting = LineageStore(root=bare_store.root, rules={"clean": [None]})
        # The original config still forbids clean as a root step.
        with pytest.raises(ValueError):
            with conflicting.create_node(step_type="clean"):
                pass

    def test_no_rules_means_anything_goes(self, tmp_path, make_node):
        store = LineageStore(root=tmp_path / "free")
        a = make_node(store, "anything")
        make_node(store, "whatever", parent=a)


class TestRules:
    def test_legal_transition_allowed(self, bare_store, make_node):
        ingest = make_node(bare_store, "ingest")
        make_node(bare_store, "clean", parent=ingest)

    def test_illegal_transition_raises(self, bare_store, make_node):
        ingest = make_node(bare_store, "ingest")
        with pytest.raises(ValueError, match="Invalid transition"):
            with bare_store.create_node(step_type="model", parent=ingest):
                pass

    def test_restricted_root_raises(self, bare_store):
        # clean's allowed parents are ["ingest"], so it cannot be a root
        with pytest.raises(ValueError, match="Invalid transition"):
            with bare_store.create_node(step_type="clean"):
                pass

    def test_unknown_parent_id_raises_clearly(self, bare_store):
        # An unknown parent id raises a clear error naming the parent — it is no
        # longer silently treated as a root (which later broke get_lineage).
        with pytest.raises(ValueError, match="not present in this store"):
            with bare_store.create_node(step_type="clean", parent="zzzzzzzz"):
                pass

    def test_unknown_parent_id_raises_even_without_a_rule(self, bare_store):
        # Also raises for a step type that has no rule, where it used to silently
        # produce a root node.
        with pytest.raises(ValueError, match="not present in this store"):
            with bare_store.create_node(step_type="freeform", parent="zzzzzzzz"):
                pass

    def test_parent_from_another_store_is_rejected(self, bare_store, tmp_path, make_node):
        other = LineageStore(tmp_path / "other")
        foreign = make_node(other, "ingest")
        with pytest.raises(ValueError, match="not present in this store"):
            with bare_store.create_node(step_type="ingest", parent=foreign):
                pass

    def test_parent_given_as_id_string_is_accepted(self, bare_store, make_node):
        ingest = make_node(bare_store, "ingest")
        child = make_node(bare_store, "clean", parent=ingest.node_id)
        assert child.parent_id == [ingest.node_id]

    def test_failed_creation_leaves_no_trace(self, bare_store):
        before = set(bare_store.database.cache)
        with pytest.raises(ValueError):
            with bare_store.create_node(step_type="model"):
                pass
        assert set(bare_store.database.cache) == before


class TestGenerations:
    def test_trigger_step_increments_generation(self, chain_store):
        store, nodes = chain_store
        assert nodes["ingest"].generation == 0
        assert nodes["clean"].generation == 1  # "clean" is a gen trigger
        assert nodes["model"].generation == 1  # non-trigger inherits

    def test_trigger_as_root_stays_generation_zero(self, tmp_path, make_node):
        store = LineageStore(root=tmp_path / "s", gen_triggers=["clean"])
        node = make_node(store, "clean")
        assert node.generation == 0


class TestGetNode:
    def test_resolves_id_string(self, chain_store):
        store, nodes = chain_store
        assert store.get_node(nodes["model"].node_id).step_type == "model"

    def test_returns_node_unchanged(self, chain_store):
        store, nodes = chain_store
        assert store.get_node(nodes["model"]) is nodes["model"]

    @pytest.mark.parametrize("bad", [None, "", "none", "None", "zzzzzzzz"])
    def test_unresolvable_inputs_return_none(self, chain_store, bad):
        store, _ = chain_store
        assert store.get_node(bad) is None

    def test_directory_without_meta_returns_none(self, bare_store):
        (bare_store.root / "rogue123").mkdir()
        assert bare_store.get_node("rogue123") is None


class TestFindNode:
    def test_match_by_step_type(self, branch_store):
        store, _ = branch_store
        assert len(store.find_node(step_type="clean")) == 2

    def test_multiple_criteria_are_anded(self, chain_store):
        store, nodes = chain_store
        found = store.find_node(step_type="ingest", source="api")
        assert [n.node_id for n in found] == [nodes["ingest"].node_id]

    def test_no_kwargs_matches_everything(self, chain_store):
        store, nodes = chain_store
        assert len(store.find_node()) == len(nodes)

    def test_callable_predicate(self, chain_store):
        store, nodes = chain_store
        found = store.find_node(accuracy=lambda a: a is not None and a > 0.8)
        assert [n.node_id for n in found] == [nodes["model"].node_id]

    def test_no_match_returns_empty(self, chain_store):
        store, _ = chain_store
        assert store.find_node(step_type="nonexistent") == []

    def test_unsearchable_meta_is_not_findable(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            node.add_meta("secret", "xyz", searchable=False)
        assert bare_store.find_node(secret="xyz") == []

    def test_found_nodes_hydrate_full_metadata(self, chain_store):
        store, _ = chain_store
        [found] = store.find_node(step_type="model")
        assert found.metadata["accuracy"]["value"] == 0.9


class TestGetMostRecent:
    def test_returns_latest_match(self, bare_store, make_node):
        make_node(bare_store, "ingest")
        newest = make_node(bare_store, "ingest")
        assert (
            bare_store.get_most_recent_node(step_type="ingest").node_id
            == newest.node_id
        )

    def test_no_match_returns_none(self, bare_store):
        assert bare_store.get_most_recent_node(step_type="model") is None


class TestLineage:
    def test_full_ancestry_oldest_first(self, chain_store):
        store, nodes = chain_store
        lineage = store.get_lineage(nodes["model"])
        assert [n.step_type for n in lineage] == ["ingest", "clean", "model"]

    def test_accepts_id_string(self, chain_store):
        store, nodes = chain_store
        assert len(store.get_lineage(nodes["model"].node_id)) == 3

    def test_unknown_id_raises_keyerror_with_guidance(self, chain_store):
        store, _ = chain_store
        with pytest.raises(KeyError, match="rebuild_db_from_disk"):
            store.get_lineage("zzzzzzzz")

    def test_sibling_branches_are_excluded(self, branch_store):
        store, nodes = branch_store
        lineage = store.get_lineage(nodes["leaf"])
        ids = {n.node_id for n in lineage}
        assert nodes["right"].node_id not in ids

    def test_find_in_lineage_scopes_to_ancestry(self, branch_store):
        store, nodes = branch_store
        found = store.find_in_lineage(nodes["leaf"], step_type="clean")
        assert [n.node_id for n in found] == [nodes["left"].node_id]


class TestFromParent:
    def test_returns_matching_parent_artifacts(self, chain_store):
        store, nodes = chain_store
        [path] = store.from_parent(nodes["model"], "clean.csv")
        assert path.name == "clean.csv"

    def test_root_node_returns_empty(self, chain_store):
        store, nodes = chain_store
        assert store.from_parent(nodes["ingest"], "data.csv") == []

    def test_unknown_node_returns_empty(self, chain_store):
        store, _ = chain_store
        assert store.from_parent("zzzzzzzz", "data.csv") == []

    def test_parent_missing_from_disk_returns_empty(self, chain_store):
        import shutil

        store, nodes = chain_store
        shutil.rmtree(nodes["clean"].path)
        assert store.from_parent(nodes["model"], "clean.csv") == []


class TestChildren:
    def test_direct_children_only(self, branch_store):
        store, nodes = branch_store
        children = {n.node_id for n in store.get_child_nodes(nodes["root"])}
        assert children == {nodes["left"].node_id, nodes["right"].node_id}

    def test_grandchildren_excluded(self, branch_store):
        store, nodes = branch_store
        children = {n.node_id for n in store.get_child_nodes(nodes["root"])}
        assert nodes["leaf"].node_id not in children

    def test_leaf_has_no_children(self, branch_store):
        store, nodes = branch_store
        assert store.get_child_nodes(nodes["leaf"]) == []

    def test_unknown_node_returns_empty(self, branch_store):
        store, _ = branch_store
        assert store.get_child_nodes("zzzzzzzz") == []


class TestPrune:
    def test_dry_run_deletes_nothing(self, branch_store):
        store, nodes = branch_store
        preview = store.prune(nodes["left"])
        assert {n.node_id for n in preview} == {
            nodes["left"].node_id,
            nodes["leaf"].node_id,
        }
        assert nodes["left"].path.exists()
        assert nodes["leaf"].node_id in store.database.cache

    def test_real_prune_removes_branch_only(self, branch_store):
        store, nodes = branch_store
        store.prune(nodes["left"], dry_run=False)
        assert not nodes["left"].path.exists()
        assert not nodes["leaf"].path.exists()
        assert nodes["right"].path.exists()
        remaining = set(store.database.cache)
        assert remaining == {nodes["root"].node_id, nodes["right"].node_id}

    def test_deleted_deepest_first(self, chain_store):
        store, nodes = chain_store
        deleted = store.prune(nodes["ingest"])
        assert [n.node_id for n in deleted] == [
            nodes["model"].node_id,
            nodes["clean"].node_id,
            nodes["ingest"].node_id,
        ]

    def test_unknown_node_returns_empty(self, chain_store):
        store, _ = chain_store
        assert store.prune("zzzzzzzz", dry_run=False) == []

    def test_pruning_store_root_is_refused(self, bare_store):
        from ancestree.models import Node

        impostor = Node(bare_store.root, "fake", 0, None, step_type="x")
        with pytest.raises(PermissionError):
            bare_store.prune(impostor, dry_run=False)

    def test_prune_handles_chains_deeper_than_recursion_limit(self, tmp_path, monkeypatch):
        # A linear lineage longer than the interpreter recursion limit must
        # prune (and preview) without RecursionError — _prune is iterative.
        # Provenance is stubbed out so building a very deep chain is fast (it
        # otherwise shells out to git per node); it is irrelevant here.
        import sys

        monkeypatch.setattr("ancestree.models.get_provenance", lambda: {})
        store = LineageStore(tmp_path / "deep", dedupe=False, chunk=False)
        depth = sys.getrecursionlimit() + 200

        with store.create_node(step_type="s") as root:
            root.add_meta("i", 0)
        prev = root.node_id
        for i in range(1, depth):
            with store.create_node(step_type="s", parent=prev) as n:
                n.add_meta("i", i)
            prev = n.node_id

        preview = store.prune(root.node_id, dry_run=True)  # was RecursionError
        assert len(preview) == depth
        assert preview[0].node_id == prev  # deepest first
        assert preview[-1].node_id == root.node_id  # target last

        deleted = store.prune(root.node_id, dry_run=False)
        assert len(deleted) == depth
        assert store.find_node() == []


class TestCrashSafety:
    def test_failure_persists_partial_work_unhealthy(self, bare_store):
        with pytest.raises(RuntimeError, match="boom"):
            with bare_store.create_node(step_type="ingest") as node:
                (node / "partial.csv").write_text("partial")
                raise RuntimeError("boom")
        reloaded = bare_store.get_node(node.node_id)
        assert reloaded.metadata["healthy"]["value"] is False
        assert [p.name for p in reloaded.artifacts()] == ["partial.csv"]

    def test_keyboard_interrupt_also_persists(self, bare_store):
        # BaseException, not just Exception: Ctrl-C must not lose evidence
        with pytest.raises(KeyboardInterrupt):
            with bare_store.create_node(step_type="ingest") as node:
                (node / "partial.csv").write_text("partial")
                raise KeyboardInterrupt
        assert bare_store.get_node(node.node_id).metadata["healthy"]["value"] is False

    def test_failure_before_any_write_leaves_no_trace(self, bare_store):
        with pytest.raises(RuntimeError):
            with bare_store.create_node(step_type="ingest") as node:
                raise RuntimeError("early")
        assert not node.path.exists()
        assert node.node_id not in bare_store.database.cache

    def test_failed_runs_are_searchable(self, bare_store, make_node):
        make_node(bare_store, "ingest")
        with pytest.raises(RuntimeError):
            with bare_store.create_node(step_type="ingest") as node:
                (node / "x.txt").write_text("x")
                raise RuntimeError
        failed = bare_store.find_node(healthy=False)
        assert [n.node_id for n in failed] == [node.node_id]


class TestAutomaticCapture:
    def test_duration_reflects_block_runtime(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            (node / "x.txt").write_text("x")
            time.sleep(0.02)
        assert node.metadata["duration_s"]["value"] >= 0.01

    def test_size_counts_artifact_bytes(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            (node / "blob.bin").write_bytes(b"x" * 2000)
        assert node.metadata["size_mb"]["value"] == round(2000 / 1e6, 6)

    def test_healthy_true_on_clean_exit(self, chain_store):
        _, nodes = chain_store
        assert nodes["model"].metadata["healthy"]["value"] is True


class TestAutoDataType:
    @pytest.fixture
    def node(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            yield node

    def test_dict_and_list_become_json(self, node):
        node.add_meta("params", {"lr": 0.1})
        node.add_meta("tags", ["a", "b"])
        assert node.metadata["params"]["data_type"] == "json"
        assert node.metadata["tags"]["data_type"] == "json"

    def test_dataframe_becomes_table(self, node):
        class DataFrame:  # is_pandas sniffs the type name + to_dict
            def to_dict(self, orient):
                return {"columns": ["a"], "index": [0], "data": [[1]]}

        node.add_meta("summary", DataFrame())
        entry = node.metadata["summary"]
        assert entry["data_type"] == "table"
        assert entry["value"] == {"columns": ["a"], "rows": [[1]]}

    def test_image_path_becomes_relativised_image(self, node):
        node.add_meta("plot", node / "fig.png")
        entry = node.metadata["plot"]
        assert entry["data_type"] == "image"
        assert entry["value"] == str(Path(node.node_id) / "fig.png")

    def test_other_path_becomes_link(self, node):
        node.add_meta("report", node / "report.pdf")
        assert node.metadata["report"]["data_type"] == "link"

    def test_url_string_becomes_link_unmangled(self, node):
        node.add_meta("dash", "https://example.com/run/1")
        entry = node.metadata["dash"]
        assert entry["data_type"] == "link"
        assert entry["value"] == "https://example.com/run/1"

    def test_plain_strings_are_never_sniffed(self, node):
        # Strings that merely look like files/paths must stay text
        node.add_meta("note", "see output.png in /tmp/results")
        assert node.metadata["note"]["data_type"] == "text"

    @pytest.mark.parametrize("value", [0.93, 42, True, None, "plain"])
    def test_scalars_stay_text(self, node, value):
        node.add_meta("v", value)
        assert node.metadata["v"]["data_type"] == "text"

    def test_explicit_type_overrides_inference(self, node):
        node.add_meta("raw", {"a": 1}, data_type="text")
        assert node.metadata["raw"]["data_type"] == "text"

    def test_json_rejects_unserialisable(self, node):
        with pytest.raises(TypeError, match="JSON-serialisable"):
            node.add_meta("bad", {"x": object()}, data_type="json")

    def test_json_rejects_non_containers(self, node):
        with pytest.raises(TypeError, match="dict or list"):
            node.add_meta("bad", "a string", data_type="json")

    def test_table_rejects_non_dataframe(self, node):
        with pytest.raises(TypeError, match="DataFrame"):
            node.add_meta("bad", [[1, 2]], data_type="table")


class TestMetadataCoercion:
    """add_meta coerces common non-JSON types (numpy/pandas scalars, datetimes,
    sets) to native Python and warns; anything uncoercible is rejected at the
    call site, not later at the meta.json write. Numpy/pandas are optional deps,
    so the numpy-shaped cases are duck-typed fakes (as test_dataframe does)."""

    @pytest.fixture
    def node(self, bare_store):
        with bare_store.create_node(step_type="ingest") as node:
            yield node

    def test_set_coerced_to_list_with_warning(self, node):
        with pytest.warns(UserWarning, match="coerced"):
            node.add_meta("tags", {3, 1, 2})
        assert sorted(node.metadata["tags"]["value"]) == [1, 2, 3]

    def test_datetime_coerced_to_isoformat(self, node):
        from datetime import datetime

        with pytest.warns(UserWarning, match="coerced"):
            node.add_meta("when", datetime(2024, 1, 2, 3, 4, 5))
        assert node.metadata["when"]["value"] == "2024-01-02T03:04:05"

    def test_numpy_like_scalar_coerced(self, node):
        class FakeScalar:  # numpy scalar shape: .item() + .dtype
            dtype = "int64"

            def item(self):
                return 42

        with pytest.warns(UserWarning, match="coerced"):
            node.add_meta("n", FakeScalar())
        assert node.metadata["n"]["value"] == 42

    def test_numpy_like_array_coerced(self, node):
        class FakeArray:  # ndarray shape: .tolist() + ndim
            dtype = "int64"
            ndim = 1

            def tolist(self):
                return [1, 2, 3]

        with pytest.warns(UserWarning, match="coerced"):
            node.add_meta("arr", FakeArray())
        assert node.metadata["arr"]["value"] == [1, 2, 3]

    def test_nested_value_is_coerced(self, node):
        with pytest.warns(UserWarning, match="coerced"):
            node.add_meta("cfg", {"vals": {1, 2}}, data_type="json")
        assert node.metadata["cfg"]["value"] == {"vals": [1, 2]}

    def test_native_values_do_not_warn(self, node, recwarn):
        node.add_meta("a", 5)
        node.add_meta("b", [1, 2, 3])
        node.add_meta("c", {"k": "v"})
        assert [w for w in recwarn if "coerced" in str(w.message)] == []

    def test_uncoercible_value_rejected_at_call_time(self, node):
        # Fires here at add_meta — not at block exit where the traceback misleads.
        with pytest.raises(TypeError, match="not JSON-serialisable even after coercion"):
            node.add_meta("bad", object())

    def test_rejected_value_leaves_no_partial_entry(self, node):
        with pytest.raises(TypeError):
            node.add_meta("bad", object())
        assert "bad" not in node.metadata


class TestWebGraph:
    def test_empty_store_generates(self, bare_store, capsys):
        bare_store.generate_web_graph()
        html = (bare_store.root / "interactive_pipeline.html").read_text()
        assert "PIPELINE_DATA" in html

    def test_graph_embeds_nodes_and_edges(self, chain_store, capsys):
        store, nodes = chain_store
        store.generate_web_graph()
        html = (store.root / "interactive_pipeline.html").read_text()
        for node in nodes.values():
            assert node.node_id in html

    def test_dangling_parent_edge_does_not_crash(self, chain_store, capsys):
        # Parent manually deleted: the child's parent_id now dangles
        import shutil

        store, nodes = chain_store
        shutil.rmtree(nodes["clean"].path)
        reopened = LineageStore(root=store.root)  # reconcile drops the parent
        reopened.generate_web_graph()
        assert (store.root / "interactive_pipeline.html").exists()
