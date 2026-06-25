"""Multi-parent (DAG) lineage: a node may have several parents (a join/merge),
so lineage is a DAG, not a tree. Covers creation, lineage, children, from_parent,
orphan-only prune, rules/generation, dedupe, the web graph, and back-compat with
the legacy single-parent on-disk format.

Run with: pytest tests/test_dag.py
"""

import pytest

from ancestree import LineageStore


@pytest.fixture
def store(tmp_path):
    return LineageStore(tmp_path / "s", dedupe=False, chunk=False)


def _n(store, step, parent=None, **meta):
    with store.create_node(step_type=step, parent=parent) as node:
        node.add_meta("step", step)
        for k, v in meta.items():
            node.add_meta(k, v)
    return node


class TestCreation:
    def test_two_parents(self, store):
        a, b = _n(store, "ingest"), _n(store, "ingest")
        m = _n(store, "merge", parent=[a, b])
        assert m.parent_id == [a.node_id, b.node_id]  # parent_id IS the list

    def test_single_parent_unchanged(self, store):
        a = _n(store, "ingest")
        c = _n(store, "clean", parent=a)
        assert c.parent_id == [a.node_id]

    def test_root_has_no_parents(self, store):
        a = _n(store, "ingest")
        assert a.parent_id == []

    def test_duplicate_parents_collapsed(self, store):
        a = _n(store, "ingest")
        m = _n(store, "merge", parent=[a, a, a.node_id])
        assert m.parent_id == [a.node_id]

    def test_parents_as_id_strings(self, store):
        a, b = _n(store, "ingest"), _n(store, "ingest")
        m = _n(store, "merge", parent=[a.node_id, b.node_id])
        assert set(m.parent_id) == {a.node_id, b.node_id}

    def test_unknown_parent_in_list_rejected(self, store):
        a = _n(store, "ingest")
        with pytest.raises(ValueError, match="not present in this store"):
            with store.create_node(step_type="merge", parent=[a, "deadbeef"]):
                pass


class TestLineage:
    def test_full_ancestor_set_topo_ordered(self, store):
        a, b = _n(store, "ingest"), _n(store, "ingest")
        ca = _n(store, "clean", parent=a)
        cb = _n(store, "clean", parent=b)
        m = _n(store, "merge", parent=[ca, cb])
        lin = [n.node_id for n in store.get_lineage(m)]
        assert set(lin) == {a.node_id, b.node_id, ca.node_id, cb.node_id, m.node_id}
        assert lin[-1] == m.node_id  # target last
        assert lin.index(a.node_id) < lin.index(ca.node_id) < lin.index(m.node_id)
        assert lin.index(b.node_id) < lin.index(cb.node_id) < lin.index(m.node_id)

    def test_shared_ancestor_listed_once_and_oldest_first(self, store):
        root = _n(store, "ingest")
        left = _n(store, "clean", parent=root)
        right = _n(store, "clean", parent=root)
        m = _n(store, "merge", parent=[left, right])
        lin = [n.node_id for n in store.get_lineage(m)]
        assert lin.count(root.node_id) == 1
        assert lin[0] == root.node_id

    def test_linear_chain_unchanged(self, store):
        a = _n(store, "ingest")
        b = _n(store, "clean", parent=a)
        c = _n(store, "model", parent=b)
        assert [n.node_id for n in store.get_lineage(c)] == [
            a.node_id, b.node_id, c.node_id
        ]

    def test_find_in_lineage_spans_the_dag(self, store):
        a = _n(store, "ingest", tag="x")
        b = _n(store, "ingest", tag="y")
        m = _n(store, "merge", parent=[a, b])
        found = {n.node_id for n in store.find_in_lineage(m, tag="y")}
        assert found == {b.node_id}


class TestChildren:
    def test_children_found_via_any_parent(self, store):
        a, b = _n(store, "ingest"), _n(store, "ingest")
        m = _n(store, "merge", parent=[a, b])
        assert m.node_id in {n.node_id for n in store.get_child_nodes(a)}
        assert m.node_id in {n.node_id for n in store.get_child_nodes(b)}


class TestFromParent:
    def test_from_parent_unions_all_parents(self, store):
        with store.create_node(step_type="ingest") as a:
            (a / "a.csv").write_text("A")
        with store.create_node(step_type="ingest") as b:
            (b / "b.csv").write_text("B")
        with store.create_node(step_type="merge", parent=[a, b]) as m:
            m.add_meta("k", 1)
        got = {p.name for p in store.from_parent(m.node_id, "*.csv")}
        assert got == {"a.csv", "b.csv"}


class TestPrune:
    def test_orphan_only_keeps_shared_descendant(self, store):
        a, b = _n(store, "ingest"), _n(store, "ingest")
        m = _n(store, "merge", parent=[a, b])
        deleted = store.prune(a, dry_run=False)
        assert {n.node_id for n in deleted} == {a.node_id}  # m survives via b
        survivor = store.get_node(m.node_id)
        assert survivor is not None
        assert survivor.parent_id == [b.node_id]  # the a-edge was dropped

    def test_deletes_descendant_once_all_parents_gone(self, store):
        a, b = _n(store, "ingest"), _n(store, "ingest")
        m = _n(store, "merge", parent=[a, b])
        store.prune(a, dry_run=False)
        deleted = store.prune(b, dry_run=False)  # now m is orphaned
        assert {n.node_id for n in deleted} == {b.node_id, m.node_id}
        assert store.get_node(m.node_id) is None

    def test_dry_run_preview_changes_nothing(self, store):
        a, b = _n(store, "ingest"), _n(store, "ingest")
        m = _n(store, "merge", parent=[a, b])
        preview = store.prune(a, dry_run=True)
        assert {n.node_id for n in preview} == {a.node_id}
        assert set(store.get_node(m.node_id).parent_id) == {a.node_id, b.node_id}

    def test_subtree_solely_supported_is_fully_deleted(self, store):
        a = _n(store, "ingest")
        c = _n(store, "clean", parent=a)
        m = _n(store, "model", parent=c)
        deleted = {n.node_id for n in store.prune(a, dry_run=False)}
        assert deleted == {a.node_id, c.node_id, m.node_id}

    def test_deep_dag_prune_no_recursion(self, store, monkeypatch):
        import sys

        monkeypatch.setattr("ancestree.models.get_provenance", lambda: {})
        prev = _n(store, "root")
        for _ in range(sys.getrecursionlimit() + 100):
            prev = _n(store, "s", parent=prev)
        deleted = store.prune(store.find_node(step="root")[0], dry_run=False)
        assert len(deleted) == sys.getrecursionlimit() + 101
        assert store.find_node() == []


class TestRulesAndGeneration:
    def test_rule_applies_to_every_parent(self, tmp_path):
        s = LineageStore(tmp_path / "s", rules={"merge": ["clean"]},
                         dedupe=False, chunk=False)
        ing = _n(s, "ingest")
        cl = _n(s, "clean", parent=ing)
        with pytest.raises(ValueError, match="Invalid transition"):
            with s.create_node(step_type="merge", parent=[cl, ing]):  # ingest not allowed
                pass

    def test_generation_is_max_of_parents(self, tmp_path):
        s = LineageStore(tmp_path / "s", gen_triggers=["gen"],
                         dedupe=False, chunk=False)
        a = _n(s, "ingest")                       # gen 0
        deep = _n(s, "gen", parent=a)             # trigger -> gen 1
        b = _n(s, "ingest")                       # gen 0
        m = _n(s, "merge", parent=[deep, b])      # max(1, 0) = 1
        assert deep.generation == 1
        assert m.generation == 1


class TestDedupeAndGraph:
    def test_dedupe_is_parent_order_independent(self, tmp_path):
        s = LineageStore(tmp_path / "s", dedupe=True, chunk=False)
        a = _n(s, "ingest", k="a")
        b = _n(s, "ingest", k="b")
        with s.create_node(step_type="merge", parent=[a, b]) as m1:
            m1.add_meta("v", 1)
        with s.create_node(step_type="merge", parent=[b, a]) as m2:  # reversed parents
            m2.add_meta("v", 1)
        assert m2.node_id == m1.node_id

    def test_web_graph_has_edge_per_parent(self, store):
        from ancestree.vis import visualise_nodes

        a, b = _n(store, "ingest"), _n(store, "ingest")
        m = _n(store, "merge", parent=[a, b])
        edges = {(e["from"], e["to"]) for e in visualise_nodes(store)["edges"]}
        assert (a.node_id, m.node_id) in edges
        assert (b.node_id, m.node_id) in edges
