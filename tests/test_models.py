"""Tests for ancestree.models.Node."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from ancestree.models import Node


@pytest.fixture
def node(tmp_path):
    """A fresh in-memory node rooted under tmp_path (meta not yet written)."""
    path = tmp_path / "abcd1234"
    path.mkdir()
    return Node(path, "abcd1234", 0, None, step_type="ingest")


class TestSpinUp:
    def test_fresh_node_builds_structural_metadata(self, node):
        meta = node.metadata
        assert meta["node_id"]["value"] == "abcd1234"
        assert meta["parent_id"]["value"] is None
        assert meta["generation"]["value"] == 0
        assert meta["step_type"]["value"] == "ingest"
        # Timestamp must be parseable ISO-8601.
        datetime.fromisoformat(meta["timestamp"]["value"])
        for entry in meta.values():
            assert entry["group"] == "Structural Properties"

    def test_existing_node_loads_metadata_from_disk(self, node):
        node.add_meta("source", "api")
        node._write_meta()

        reloaded = Node(node.path, "abcd1234", 0, None, step_type="ingest")
        assert reloaded.metadata == node.metadata

    def test_metadata_property_returns_defensive_copy(self, node):
        snapshot = node.metadata
        snapshot["node_id"]["value"] = "tampered"
        assert node.metadata["node_id"]["value"] == "abcd1234"


class TestAddMeta:
    def test_default_entry_shape(self, node):
        node.add_meta("accuracy", 0.95)
        entry = node.metadata["accuracy"]
        assert entry == {
            "value": 0.95,
            "type": "text",
            "group": None,
            "searchable": True,
        }

    def test_group_and_searchable_are_stored(self, node):
        node.add_meta("secret", "xyz", group="Internal", searchable=False)
        entry = node.metadata["secret"]
        assert entry["group"] == "Internal"
        assert entry["searchable"] is False

    def test_same_key_overwrites(self, node):
        node.add_meta("status", "running")
        node.add_meta("status", "done")
        assert node.metadata["status"]["value"] == "done"

    def test_image_value_is_relativised_to_store_root(self, node):
        absolute = node.path / "plots" / "fig.png"
        node.add_meta("figure", str(absolute), type="image")
        assert node.metadata["figure"]["value"] == str(Path("abcd1234/plots/fig.png"))

    def test_image_value_outside_store_is_unchanged(self, node):
        node.add_meta("figure", "/elsewhere/fig.png", type="image")
        assert node.metadata["figure"]["value"] == str(Path("/elsewhere/fig.png"))


class TestWriteMeta:
    def test_writes_meta_json_and_cleans_temp_file(self, node):
        node._write_meta()
        meta_path = node.path / "meta.json"
        assert meta_path.exists()
        assert not (node.path / "meta.json.tmp").exists()
        assert json.loads(meta_path.read_text()) == node.metadata

    def test_overwrites_previous_contents(self, node):
        node._write_meta()
        node.add_meta("late_addition", 42)
        node._write_meta()
        on_disk = json.loads((node.path / "meta.json").read_text())
        assert on_disk["late_addition"]["value"] == 42


class TestToDb:
    def test_flattens_to_key_value_pairs(self, node):
        node.add_meta("accuracy", 0.95)
        flat = node.to_db()
        assert flat["node_id"] == "abcd1234"
        assert flat["step_type"] == "ingest"
        assert flat["accuracy"] == 0.95

    def test_excludes_unsearchable_entries(self, node):
        node.add_meta("secret", "xyz", searchable=False)
        assert "secret" not in node.to_db()


class TestArtifacts:
    @pytest.fixture
    def populated(self, node):
        (node / "data.csv").write_text("csv")
        (node / "results/deep.txt").write_text("txt")
        node._write_meta()  # meta.json must never count as an artifact
        return node

    def test_lists_all_files_except_meta_json(self, populated):
        found = {str(p) for p in populated.artifacts()}
        assert found == {
            str(Path("abcd1234/data.csv")),
            str(Path("abcd1234/results/deep.txt")),
        }

    def test_glob_pattern_filter(self, populated):
        found = populated.artifacts("*.csv")
        assert [p.name for p in found] == ["data.csv"]

    def test_case_insensitive_substring_filter(self, populated):
        found = populated.artifacts("DEEP")
        assert [p.name for p in found] == ["deep.txt"]

    def test_no_match_returns_empty(self, populated):
        assert populated.artifacts("nomatch") == []

    def test_empty_node_returns_empty(self, node):
        assert node.artifacts() == []


class TestPathOperator:
    def test_truediv_builds_path_under_node(self, node):
        target = node / "out.csv"
        assert target == node.path / "out.csv"

    def test_truediv_accepts_path_objects(self, node):
        target = node / Path("sub") / "file.txt"
        assert target == node.path / "sub" / "file.txt"

    def test_truediv_creates_parent_directories(self, node):
        # Pins the documented side effect: composing a path creates its
        # parent directories so the returned path is immediately writable.
        node / "nested/dirs/file.txt"
        assert (node.path / "nested" / "dirs").is_dir()


class TestRepr:
    def test_repr_contains_id_and_generation(self, node):
        text = repr(node)
        assert "abcd1234" in text
        assert "generation = 0" in text
