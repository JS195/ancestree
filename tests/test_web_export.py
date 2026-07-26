"""Phase 7: the view-only static export (issue #18). Rewrites the coverage
of test_vis.py against the new backend: the graph payload, validated
templating, artifact materialization / data-URI inlining, and script-safe
JSON embedding."""

from pathlib import Path

import pytest

from ancestree.store import LineageStore
from ancestree.web.graph import build_graph, node_detail

_TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # fake but suffix-typed


@pytest.fixture()
def store(tmp_path: Path) -> LineageStore:
    return LineageStore(tmp_path / "proj")


def _chain(store: LineageStore) -> None:
    with store.create_node(step_type="ingest") as a:
        (a / "raw.csv").write_text("a,b\n1,2\n")
        a.add_meta("rows", 1)
    with store.create_node(step_type="clean", parent=a) as b:
        (b / "figs/plot.png").write_bytes(_TINY_PNG)
        b.add_meta("confusion", b / "figs/plot.png", group="Figures")
        b.add_meta("params", {"lr": 0.1})
    with store.create_node(step_type="model", parent=b) as c:
        c.add_meta("accuracy", 0.9)
        c.add_meta("note", "</script><b>sneaky</b>")


def test_graph_payload_levels_and_edges(store: LineageStore) -> None:
    _chain(store)
    payload = build_graph(store)
    levels = {node["id"]: node["level"] for node in payload["nodes"]}
    groups = {node["group"] for node in payload["nodes"]}
    assert groups == {"ingest", "clean", "model"}
    assert sorted(levels.values()) == [0, 1, 2]
    assert len(payload["edges"]) == 2


def test_node_detail_contents(store: LineageStore) -> None:
    _chain(store)
    model = store.latest(step_type="model")
    assert model is not None
    detail = node_detail(store, model.node_id)
    assert detail["step_type"] == "model"
    assert detail["healthy"] is True
    assert detail["metadata"]["accuracy"]["value"] == 0.9
    assert set(detail["provenance"]) == {
        "user",
        "python_version",
        "platform",
        "git_commit",
        "git_dirty",
        "git_branch",
    }
    clean = store.latest(step_type="clean")
    assert clean is not None
    arts = node_detail(store, clean.node_id)["artifacts"]
    assert [a["relpath"] for a in arts] == ["figs/plot.png"]


def test_export_writes_a_complete_offline_file(
    store: LineageStore, tmp_path: Path
) -> None:
    _chain(store)
    dest = store.export_graph()
    assert dest == tmp_path / "proj" / "interactive_pipeline.html"
    html = dest.read_text()

    # Validated markers were substituted, never left behind.
    assert "__ANCESTREE_GRAPH_JSON__" not in html
    assert "__ANCESTREE_VIS_JS__" not in html
    assert "vis" in html and '"details"' in html

    # The csv is materialized beside the file; the small png is inlined.
    ingest = store.latest(step_type="ingest")
    assert ingest is not None
    materialized = (
        tmp_path / "proj" / "interactive_pipeline_files" / ingest.node_id / "raw.csv"
    )
    assert materialized.read_text() == "a,b\n1,2\n"
    assert f"interactive_pipeline_files/{ingest.node_id}/raw.csv" in html
    assert "data:image/png;base64," in html


def test_inline_only_stores_stay_single_file(
    store: LineageStore, tmp_path: Path
) -> None:
    with store.create_node(step_type="ingest") as node:
        (node / "plot.png").write_bytes(_TINY_PNG)
    dest = store.export_graph()
    assert "data:image/png;base64," in dest.read_text()
    assert not (tmp_path / "proj" / "interactive_pipeline_files").exists()


def test_metadata_only_export_skips_artifact_copies(
    store: LineageStore, tmp_path: Path
) -> None:
    _chain(store)
    dest = store.export_graph(include_artifacts=False)
    html = dest.read_text()
    assert not (tmp_path / "proj" / "interactive_pipeline_files").exists()
    assert "data:image/png;base64," not in html
    assert '"raw.csv"' in html  # still listed, just not linked


def test_embedded_json_cannot_break_out_of_its_script_tag(
    store: LineageStore,
) -> None:
    _chain(store)
    html = store.export_graph().read_text()
    # The malicious metadata value survives as data but its "</" is
    # escaped, so the JSON block cannot terminate the script element.
    assert "<\\/script><b>sneaky<\\/b>" in html
    assert "</script><b>sneaky" not in html


def test_export_to_custom_destination(store: LineageStore, tmp_path: Path) -> None:
    _chain(store)
    dest = store.export_graph(dest=tmp_path / "out" / "snap.html")
    assert dest.exists()
    ingest = store.latest(step_type="ingest")
    assert ingest is not None
    assert (tmp_path / "out" / "snap_files" / ingest.node_id / "raw.csv").exists()
