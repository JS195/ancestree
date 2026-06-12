"""Tests for ancestree.vis (graph data extraction and HTML generation)."""
from pathlib import Path

from ancestree.vis import assign_levels, run_web_generator, visualise_nodes


class TestAssignLevels:
    def test_linear_chain(self):
        levels = assign_levels(["a", "b", "c"], [("a", "b"), ("b", "c")])
        assert levels == {"a": 0, "b": 1, "c": 2}

    def test_branching(self):
        levels = assign_levels(
            ["a", "b", "c", "d"],
            [("a", "b"), ("a", "c"), ("b", "d")],
        )
        assert levels["a"] == 0
        assert levels["b"] == 1
        assert levels["c"] == 1
        assert levels["d"] == 2

    def test_no_edges_means_all_roots(self):
        assert assign_levels(["a", "b"], []) == {"a": 0, "b": 0}

    def test_diamond_takes_longest_path(self):
        levels = assign_levels(
            ["a", "b", "c", "d"],
            [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        )
        assert levels["d"] == 2


class TestVisualiseNodes:
    def test_chain_produces_nodes_and_edges(self, chain_store):
        store, nodes = chain_store
        graph = visualise_nodes(store)

        ids = {n["id"] for n in graph["nodes"]}
        assert ids == {n.node_id for n in nodes.values()}

        edges = {(e["from"], e["to"]) for e in graph["edges"]}
        assert edges == {
            (nodes["ingest"].node_id, nodes["clean"].node_id),
            (nodes["clean"].node_id, nodes["model"].node_id),
        }

    def test_levels_follow_lineage_depth(self, chain_store):
        store, nodes = chain_store
        graph = visualise_nodes(store)
        levels = {n["id"]: n["level"] for n in graph["nodes"]}
        assert levels[nodes["ingest"].node_id] == 0
        assert levels[nodes["clean"].node_id] == 1
        assert levels[nodes["model"].node_id] == 2

    def test_node_label_and_group_use_step_type(self, chain_store):
        store, nodes = chain_store
        graph = visualise_nodes(store)
        by_id = {n["id"]: n for n in graph["nodes"]}
        clean = by_id[nodes["clean"].node_id]
        assert clean["group"] == "clean"
        assert "clean" in clean["label"]
        assert nodes["clean"].node_id in clean["label"]

    def test_artifacts_appear_as_link_entries(self, chain_store):
        store, nodes = chain_store
        graph = visualise_nodes(store)
        by_id = {n["id"]: n for n in graph["nodes"]}
        entries = by_id[nodes["ingest"].node_id]["entries"]
        assert entries["data.csv"]["data_type"] == "link"
        assert entries["data.csv"]["group"] == "Artifacts"
        assert entries["data.csv"]["value"] == str(
            Path(nodes["ingest"].node_id) / "data.csv"
        )

    def test_stray_directory_is_skipped(self, chain_store):
        store, nodes = chain_store
        (store.root / "not_a_node").mkdir()
        graph = visualise_nodes(store)
        assert len(graph["nodes"]) == len(nodes)

    def test_files_in_root_are_skipped(self, chain_store):
        store, nodes = chain_store
        (store.root / "notes.txt").write_text("hello")
        graph = visualise_nodes(store)
        assert len(graph["nodes"]) == len(nodes)

    def test_empty_store_produces_empty_graph(self, bare_store):
        graph = visualise_nodes(bare_store)
        assert graph == {"nodes": [], "edges": []}


class TestRunWebGenerator:
    def test_writes_html_into_store_root(self, chain_store):
        store, _ = chain_store
        location = run_web_generator(store)
        assert Path(location) == store.root / "interactive_pipeline.html"
        assert Path(location).exists()

    def test_placeholders_and_asset_references_are_replaced(self, chain_store):
        store, nodes = chain_store
        html = Path(run_web_generator(store)).read_text()

        assert "{{PYTHON_NODES}}" not in html
        assert "{{PYTHON_EDGES}}" not in html
        # The external asset references must be replaced by inlined content
        # so the file is fully standalone.
        assert 'src="../../web_app/vis-network.min.js"' not in html
        assert 'href ="../../web_app/styles.css"' not in html
        assert 'src="../../web_app/actions.js"' not in html

        for node in nodes.values():
            assert node.node_id in html
