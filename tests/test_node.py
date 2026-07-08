"""Phase 4: the Node record / recording-handle split and the metadata
envelope semantics (issue #15). Rewrites the coverage of test_models.py
against the new API."""

import dataclasses
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ancestree.domain.node import Node
from ancestree.errors import ArtifactNotFound
from ancestree.store import LineageStore


class DataFrame:
    """Duck-typed stand-in for pandas.DataFrame — is_pandas checks the
    type name and to_dict, so this exercises the 'table' path without a
    pandas dependency."""

    def __init__(self, columns: List[str], rows: List[List[Any]]) -> None:
        self._columns = columns
        self._rows = rows

    def to_dict(self, orient: str = "split") -> Dict[str, Any]:
        assert orient == "split"
        return {
            "columns": self._columns,
            "index": list(range(len(self._rows))),
            "data": self._rows,
        }


@pytest.fixture()
def store(tmp_path: Path) -> LineageStore:
    return LineageStore(tmp_path / "proj")


def _node_with_artifacts(store: LineageStore) -> Node:
    with store.create_node(step_type="ingest") as node:
        (node / "sample.csv").write_text("a,b\n")
        (node / "deep/model.bin").write_bytes(b"\x00" * 1024)
        (node / "README.md").write_text("hi")
        node.add_meta("rows", 10, group="Metrics")
        node.add_meta("notes", "internal", searchable=False)
    resolved = store.get(node)
    assert resolved is not None
    return resolved


# ---------------------------------------------------------------------------
# The record: immutable, hashable, store-backed
# ---------------------------------------------------------------------------


def test_record_is_immutable_and_hashable(store: LineageStore) -> None:
    node = _node_with_artifacts(store)
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.step_type = "hacked"  # type: ignore[misc]

    again = store.get(node.node_id)
    assert again == node  # equality by node_id
    assert len({node, again}) == 1  # hashable value object
    assert "add_meta" not in dir(node)  # the 0.1.x footgun is a type error


def test_record_metadata_and_provenance(store: LineageStore) -> None:
    node = _node_with_artifacts(store)

    meta = node.metadata
    assert meta["rows"] == {
        "value": 10,
        "data_type": "text",
        "group": "Metrics",
        "searchable": True,
    }
    assert meta["notes"]["searchable"] is False
    assert "node_id" not in meta  # structural facts are attributes

    prov = node.provenance
    assert set(prov) == {
        "user",
        "python_version",
        "platform",
        "git_commit",
        "git_dirty",
        "git_branch",
    }


def test_record_artifacts_matching_and_read(store: LineageStore) -> None:
    node = _node_with_artifacts(store)

    all_paths = node.artifacts()
    assert [p.name for p in all_paths] == ["README.md", "model.bin", "sample.csv"]
    assert [p.name for p in node.artifacts("*.csv")] == ["sample.csv"]
    assert [p.name for p in node.artifacts("SAMPLE")] == ["sample.csv"]
    assert [p.name for p in node.artifacts("deep/*.bin")] == ["model.bin"]

    # Read-side `/` returns a readable path, reassembled from the store.
    assert (node / "sample.csv").read_text() == "a,b\n"
    assert (node / "deep/model.bin").read_bytes() == b"\x00" * 1024
    with pytest.raises(ArtifactNotFound):
        node / "ghost.csv"


# ---------------------------------------------------------------------------
# The handle: write-side `/` and add_meta
# ---------------------------------------------------------------------------


def test_handle_write_paths_and_guards(store: LineageStore) -> None:
    with store.create_node(step_type="ingest") as node:
        target = node / "results/out.csv"
        assert target.parent.is_dir()  # ready to write immediately
        target.write_text("x")
        with pytest.raises(ValueError, match="escapes"):
            node / "../evil.txt"
        assert [p.name for p in node.artifacts("*.csv")] == ["out.csv"]


def test_add_meta_validation(store: LineageStore) -> None:
    with store.create_node(step_type="ingest") as node:
        node.add_meta("keep", 1)  # touch it so no discard warning
        with pytest.raises(ValueError, match="reserved"):
            node.add_meta("step_type", "sneaky")
        with pytest.raises(ValueError, match="Invalid data_type"):
            node.add_meta("k", 1, data_type="fancy")
        with pytest.raises(TypeError, match="dict or list"):
            node.add_meta("k", 42, data_type="json")
        with pytest.raises(TypeError, match="pandas DataFrame"):
            node.add_meta("k", 42, data_type="table")
        with pytest.raises(TypeError, match="not JSON-serialisable"):
            node.add_meta("k", object())


def test_add_meta_type_inference_and_coercion(store: LineageStore) -> None:
    with store.create_node(step_type="ingest") as node:
        node.add_meta("params", {"lr": 0.1})  # dict -> json, display-only
        node.add_meta("paper", "https://example.org/x")  # url -> link
        node.add_meta("accuracy", 0.9)  # scalar -> text, searchable
        node.add_meta("query", "SELECT 1", data_type="code")
        with pytest.warns(UserWarning, match="coerced"):
            node.add_meta("tags", {"a", "a"})  # set -> list, with a warning
        node.add_meta("accuracy", 0.95)  # overwrite wins

    meta = store.get(node).metadata  # type: ignore[union-attr]
    assert meta["params"]["data_type"] == "json"
    assert meta["params"]["searchable"] is False
    assert meta["paper"]["data_type"] == "link"
    assert meta["paper"]["searchable"] is True  # urls stay searchable
    assert meta["accuracy"] == {
        "value": 0.95,
        "data_type": "text",
        "group": "General",
        "searchable": True,
    }
    assert meta["query"]["data_type"] == "code"
    assert meta["tags"]["value"] == ["a"]


def test_add_meta_table_via_duck_typed_dataframe(store: LineageStore) -> None:
    frame = DataFrame(["x", "y"], [[1, 2], [3, 4]])
    with store.create_node(step_type="ingest") as node:
        node.add_meta("results", frame, data_type="table")
    meta = store.get(node).metadata  # type: ignore[union-attr]
    assert meta["results"]["value"] == {"columns": ["x", "y"], "rows": [[1, 2], [3, 4]]}
    assert meta["results"]["searchable"] is False


def test_add_meta_normalises_artifact_references(store: LineageStore) -> None:
    with store.create_node(step_type="ingest") as node:
        image = node / "figs/confusion.png"
        image.write_bytes(b"\x89PNG fake")
        node.add_meta("confusion_matrix", image, group="Figures")

    meta = store.get(node).metadata  # type: ignore[union-attr]
    entry = meta["confusion_matrix"]
    assert entry["data_type"] == "image"
    assert entry["value"] == "figs/confusion.png"  # node-relative, durable
    assert entry["searchable"] is False
