"""Phase 8: the live server (issue #19) — SQL-backed endpoints, the query
grammar, explorer-parity payloads (diff, runs table), DB-keyed artifact
serving, and lifecycle."""

import json
from pathlib import Path
from typing import Iterator, Tuple
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from ancestree.store import LineageStore
from ancestree.web.server import search_ids, start_server

Env = Tuple[LineageStore, str]


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[Env]:
    store = LineageStore(
        tmp_path / "proj", rules={"clean": ["ingest"], "model": ["clean"]}
    )
    with store.create_node(step_type="ingest") as ingest:
        (ingest / "raw.csv").write_text("a,b\n1,2\n")
        ingest.add_meta("rows", 1)
    with store.create_node(step_type="clean", parent=ingest) as clean:
        clean.add_meta("accuracy", 0.91)
    with store.create_node(step_type="model", parent=clean) as model:
        model.add_meta("accuracy", 0.97)

    handle = start_server(store)
    try:
        yield store, handle.url
    finally:
        handle.close()
        store.close()


def _get_json(url: str) -> dict:
    with urlopen(url) as response:
        return json.loads(response.read())


def test_index_serves_the_classic_explorer(served: Env) -> None:
    store, url = served
    with urlopen(url + "/") as response:
        html = response.read().decode()
    assert response.status == 200
    assert "window.PIPELINE_DATA" in html  # the classic template's payload
    assert "{{PYTHON_NODES}}" not in html  # data markers substituted
    assert "{{PYTHON_EDGES}}" not in html
    # The old repo's relative asset references must be inlined.
    assert "../../web_app/" not in html
    for node in store.find():
        assert node.node_id in html


def test_index_rerenders_on_each_request(served: Env) -> None:
    store, url = served
    with store.create_node(step_type="ingest") as late:
        late.add_meta("rows", 9)
    with urlopen(url + "/") as response:
        html = response.read().decode()
    assert late.node_id in html  # created after the server started


def test_api_graph_returns_the_skeleton(served: Env) -> None:
    _, url = served
    graph = _get_json(url + "/api/graph")
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 2
    assert {node["group"] for node in graph["nodes"]} == {
        "ingest",
        "clean",
        "model",
    }


def test_api_node_detail_and_404(served: Env) -> None:
    store, url = served
    model = store.latest(step_type="model")
    assert model is not None
    detail = _get_json(url + f"/api/node/{model.node_id}")
    assert detail["metadata"]["accuracy"]["value"] == 0.97
    assert detail["parent_id"] == list(model.parent_id)

    with pytest.raises(HTTPError) as error:
        urlopen(url + "/api/node/deadbeef")
    assert error.value.code == 404


def test_search_grammar_is_answered_by_sql(served: Env) -> None:
    store, url = served

    def ids(query: str) -> list:
        from urllib.parse import quote

        return _get_json(url + "/api/search?q=" + quote(query))["ids"]

    model = store.latest(step_type="model")
    clean = store.latest(step_type="clean")
    assert model is not None and clean is not None

    assert ids("step_type=model") == [model.node_id]
    assert set(ids("accuracy>0.9")) == {clean.node_id, model.node_id}
    assert ids("accuracy>0.95") == [model.node_id]
    assert ids("accuracy<=0.91") == [clean.node_id]
    assert ids("step_type=model accuracy>0.9") == [model.node_id]  # AND
    assert model.node_id in ids("model")  # free text
    assert len(ids("")) == 3  # an empty query matches everything
    assert ids("accuracy>notanumber") == []
    # The same grammar is importable server-side — one implementation.
    assert search_ids(store, "healthy=true accuracy>0.95") == [model.node_id]


def test_api_diff_aligns_and_deltas(served: Env) -> None:
    store, url = served
    clean = store.latest(step_type="clean")
    model = store.latest(step_type="model")
    assert clean is not None and model is not None
    diff = _get_json(
        url + f"/api/diff?a={clean.node_id}&b={model.node_id}"
    )
    rows = {row["key"]: row for row in diff["rows"]}
    assert rows["step_type"]["same"] is False
    accuracy = rows["accuracy"]
    assert accuracy["a"] == 0.91 and accuracy["b"] == 0.97
    assert accuracy["same"] is False
    assert abs(accuracy["delta"] - 0.06) < 1e-9

    with pytest.raises(HTTPError) as error:
        urlopen(url + "/api/diff?a=onlyone")
    assert error.value.code == 400


def test_api_runs_builds_the_table(served: Env) -> None:
    _, url = served
    runs = _get_json(url + "/api/runs")
    assert runs["columns"] == ["accuracy", "rows"]
    assert len(runs["rows"]) == 3
    by_step = {row["step_type"]: row for row in runs["rows"]}
    assert by_step["model"]["metrics"]["accuracy"] == 0.97
    assert by_step["ingest"]["healthy"] is True


def test_artifacts_are_served_by_database_key(served: Env) -> None:
    store, url = served
    ingest = store.latest(step_type="ingest")
    assert ingest is not None

    with urlopen(url + f"/api/artifact/{ingest.node_id}/raw.csv") as response:
        assert response.read() == b"a,b\n1,2\n"
        assert response.headers["Content-Type"] == "text/csv"

    with pytest.raises(HTTPError) as error:
        urlopen(url + f"/api/artifact/{ingest.node_id}/ghost.csv")
    assert error.value.code == 404

    # A traversal-shaped request is just a key that matches nothing: the
    # endpoint never touches the filesystem.
    with pytest.raises(HTTPError) as error:
        urlopen(url + f"/api/artifact/{ingest.node_id}/../../etc/passwd")
    assert error.value.code == 404


def test_template_artifact_links_resolve(served: Env) -> None:
    # The classic template links each artifact as "<node_id>/<relpath>".
    store, url = served
    ingest = store.latest(step_type="ingest")
    assert ingest is not None
    with urlopen(url + f"/{ingest.node_id}/raw.csv") as response:
        assert response.read() == b"a,b\n1,2\n"


def test_unknown_route_is_404_and_close_is_clean(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "proj")
    with store.create_node(step_type="ingest") as node:
        node.add_meta("ok", True)
    handle = start_server(store)
    with pytest.raises(HTTPError) as error:
        urlopen(handle.url + "/api/nope")
    assert error.value.code == 404
    handle.close()
    store.close()


def test_host_live_graph_nonblocking_closes_with_store(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "proj")
    with store.create_node(step_type="ingest") as node:
        node.add_meta("ok", True)
    url = store.host_live_graph(block=False, open_browser=False)
    assert _get_json(url + "/api/graph")["nodes"]
    store.close()  # shuts the server down too
    with pytest.raises(Exception):
        urlopen(url + "/api/graph", timeout=1)


def test_host_live_graph_rerun_replaces_previous_server(tmp_path: Path) -> None:
    # Dash-style: re-running the cell restarts the server, no leaked thread.
    store = LineageStore(tmp_path / "proj")
    with store.create_node(step_type="ingest") as node:
        node.add_meta("ok", True)
    first = store.host_live_graph(block=False, open_browser=False)
    second = store.host_live_graph(block=False, open_browser=False)
    assert _get_json(second + "/api/graph")["nodes"]
    with pytest.raises(Exception):
        urlopen(first + "/api/graph", timeout=1)
    store.close()