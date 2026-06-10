"""Tests for ancestree.database.lineage_database.

These tests set shelf state directly through the database API (or raw files
on disk for rebuild tests) so they stay independent of LineageStore.
"""
import json
from datetime import datetime, timezone

import pytest

from ancestree.database import lineage_database


@pytest.fixture
def db(tmp_path):
    return lineage_database(tmp_path)


def _entry(node_id, parent_id=None, step_type="step", ts="2026-01-01T00:00:00+00:00", **extra):
    record = {
        "node_id": node_id,
        "parent_id": parent_id,
        "step_type": step_type,
        "timestamp": ts,
    }
    record.update(extra)
    return record


class TestAddRemove:
    def test_add_then_find_roundtrip(self, db):
        db.add("aaaa0001", _entry("aaaa0001"))
        assert db.find_matches(node_id="aaaa0001") == ["aaaa0001"]

    def test_add_overwrites_existing_key(self, db):
        db.add("aaaa0001", _entry("aaaa0001", step_type="old"))
        db.add("aaaa0001", _entry("aaaa0001", step_type="new"))
        assert db.find_matches(step_type="old") == []
        assert db.find_matches(step_type="new") == ["aaaa0001"]

    def test_remove_deletes_entry(self, db):
        db.add("aaaa0001", _entry("aaaa0001"))
        db.remove("aaaa0001")
        assert db.find_matches(node_id="aaaa0001") == []

    def test_remove_missing_key_raises(self, db):
        # Pins current behaviour: removing an unindexed id is a KeyError.
        db.add("aaaa0001", _entry("aaaa0001"))
        with pytest.raises(KeyError):
            db.remove("bbbb0002")


class TestFindMatches:
    def test_fresh_database_returns_empty(self, db):
        assert db.find_matches(step_type="anything") == []

    def test_multiple_criteria_are_anded(self, db):
        db.add("a", _entry("a", step_type="clean", parent_id="r"))
        db.add("b", _entry("b", step_type="clean", parent_id="x"))
        assert db.find_matches(step_type="clean", parent_id="r") == ["a"]

    def test_callable_matcher(self, db):
        db.add("a", _entry("a", accuracy=0.9))
        db.add("b", _entry("b", accuracy=0.5))
        assert db.find_matches(accuracy=lambda v: v is not None and v > 0.8) == ["a"]

    def test_callable_raising_is_treated_as_no_match(self, db):
        db.add("a", _entry("a"))  # no "accuracy" key -> comparison raises
        assert db.find_matches(accuracy=lambda v: v > 0.8) == []

    def test_none_matches_missing_keys_too(self, db):
        # Pins a known quirk: searching for None matches both records whose
        # value is explicitly None and records lacking the key entirely.
        db.add("root", _entry("root", parent_id=None))
        db.add("child", _entry("child", parent_id="root"))
        db.add("keyless", {"node_id": "keyless"})
        assert set(db.find_matches(parent_id=None)) == {"root", "keyless"}


class TestGetLineage:
    def _chain(self, db):
        db.add("a", _entry("a", parent_id=None))
        db.add("b", _entry("b", parent_id="a"))
        db.add("c", _entry("c", parent_id="b"))

    def test_chain_is_returned_oldest_first(self, db):
        self._chain(db)
        assert db.get_lineage("c") == ["a", "b", "c"]

    def test_root_lineage_is_single_entry(self, db):
        self._chain(db)
        assert db.get_lineage("a") == ["a"]

    def test_unknown_node_raises_key_error_with_guidance(self, db):
        self._chain(db)
        with pytest.raises(KeyError, match="not found in the index"):
            db.get_lineage("zzzz9999")

    def test_orphaned_node_raises_key_error(self, db):
        db.add("child", _entry("child", parent_id="ghost"))
        with pytest.raises(KeyError, match="'ghost' not found in the index"):
            db.get_lineage("child")

    def test_cycle_raises_value_error(self, db):
        db.add("a", _entry("a", parent_id="b"))
        db.add("b", _entry("b", parent_id="a"))
        with pytest.raises(ValueError, match="Cycle detected"):
            db.get_lineage("a")

    def test_self_cycle_raises_value_error(self, db):
        db.add("a", _entry("a", parent_id="a"))
        with pytest.raises(ValueError, match="Cycle detected"):
            db.get_lineage("a")


class TestGetMostRecent:
    def test_returns_latest_timestamp(self, db):
        db.add("old", _entry("old", ts="2026-01-01T00:00:00+00:00"))
        db.add("new", _entry("new", ts="2026-06-01T00:00:00+00:00"))
        db.add("mid", _entry("mid", ts="2026-03-01T00:00:00+00:00"))
        assert db.get_most_recent() == "new"

    def test_respects_filter_kwargs(self, db):
        db.add("old_clean", _entry("old_clean", step_type="clean", ts="2026-01-01T00:00:00+00:00"))
        db.add("new_model", _entry("new_model", step_type="model", ts="2026-06-01T00:00:00+00:00"))
        assert db.get_most_recent(step_type="clean") == "old_clean"

    def test_no_match_returns_none(self, db):
        db.add("a", _entry("a"))
        assert db.get_most_recent(step_type="zzz") is None

    def test_empty_database_returns_none(self, db):
        assert db.get_most_recent() is None

    def test_record_without_timestamp_raises(self, db):
        # Pins current behaviour (arguably a bug): a matching record with no
        # timestamp crashes the query instead of being skipped.
        db.add("a", {"node_id": "a", "step_type": "clean"})
        with pytest.raises(TypeError):
            db.get_most_recent(step_type="clean")


class TestRebuildFromDisk:
    @staticmethod
    def _write_node_dir(root, node_id, nested_meta):
        node_dir = root / node_id
        node_dir.mkdir()
        (node_dir / "meta.json").write_text(json.dumps(nested_meta))

    def test_rebuild_flattens_nested_metadata(self, tmp_path):
        self._write_node_dir(tmp_path, "aaaa0001", {
            "node_id": {"value": "aaaa0001", "type": "text", "group": None, "searchable": True},
            "step_type": {"value": "ingest", "type": "text", "group": None, "searchable": True},
        })
        db = lineage_database(tmp_path)
        db.rebuild_from_disk()
        assert db.find_matches(step_type="ingest") == ["aaaa0001"]

    def test_rebuild_excludes_unsearchable_entries(self, tmp_path):
        self._write_node_dir(tmp_path, "aaaa0001", {
            "node_id": {"value": "aaaa0001", "searchable": True},
            "secret": {"value": "xyz", "searchable": False},
        })
        db = lineage_database(tmp_path)
        db.rebuild_from_disk()
        assert db.find_matches(secret="xyz") == []
        assert db.find_matches(node_id="aaaa0001") == ["aaaa0001"]

    def test_rebuild_skips_non_dict_values(self, tmp_path):
        self._write_node_dir(tmp_path, "aaaa0001", {
            "node_id": {"value": "aaaa0001"},
            "legacy_flat_key": "plain string",
        })
        db = lineage_database(tmp_path)
        db.rebuild_from_disk()
        assert db.find_matches(legacy_flat_key="plain string") == []

    def test_rebuild_with_no_nodes_creates_empty_index(self, tmp_path):
        db = lineage_database(tmp_path)
        db.rebuild_from_disk()
        assert db.find_matches(node_id="anything") == []
