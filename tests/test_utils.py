"""Tests for ancestree.utils."""
from datetime import datetime
from pathlib import Path

import pytest

from ancestree.utils import (
    _finditem,
    format_metadata,
    get_provenance,
    is_match,
    is_pandas,
    parse_iso_utc,
    parse_time,
    safe_get_user,
)


class FakeDataFrame:
    """Duck-types just enough of pandas for is_pandas/format_metadata."""

    def to_dict(self, orient=None):
        return {"columns": ["a", "b"], "data": [[1, 2], [3, 4]], "index": [0, 1]}


# Rename so type(obj).__name__ == "DataFrame", which is what is_pandas sniffs.
FakeDataFrame.__name__ = "DataFrame"


class TestIsPandas:
    def test_accepts_dataframe_like_object(self):
        assert is_pandas(FakeDataFrame()) is True

    def test_rejects_other_objects(self):
        assert is_pandas("DataFrame") is False
        assert is_pandas({"to_dict": 1}) is False
        assert is_pandas(None) is False


class TestFormatMetadata:
    def test_text_passthrough(self):
        result = format_metadata("text", "Task complete", label="Status")
        assert result == {"type": "text", "content": "Task complete", "label": "Status"}

    def test_label_defaults_to_none(self):
        assert format_metadata("text", "x")["label"] is None

    def test_data_type_is_lowercased(self):
        assert format_metadata("TEXT", "x")["type"] == "text"

    def test_list_passthrough(self):
        result = format_metadata("list", [1, 2, 3])
        assert result["content"] == [1, 2, 3]

    def test_table_from_dataframe(self):
        result = format_metadata("table", FakeDataFrame())
        assert result["content"] == {"columns": ["a", "b"], "rows": [[1, 2], [3, 4]]}

    def test_table_rejects_non_dataframe(self):
        with pytest.raises(TypeError, match="Expected a pandas DataFrame"):
            format_metadata("table", [[1, 2]])

    def test_image_path_trimmed_to_node_id_component(self):
        result = format_metadata("image", "/abs/store/abcd1234/plots/fig.png")
        assert result["content"] == str(Path("abcd1234/plots/fig.png"))

    def test_image_path_without_node_id_is_unchanged(self):
        result = format_metadata("image", "plots/fig.png")
        assert result["content"] == "plots/fig.png"


class TestFinditem:
    def test_returns_value_from_flat_dict(self):
        assert _finditem({"a": 1, "b": 2}, "b") == 2

    def test_missing_key_returns_none(self):
        assert _finditem({"a": 1}, "z") is None

    def test_searches_list_of_dicts(self):
        assert _finditem([{"a": 1}, {"b": 2}], "b") == 2

    def test_non_container_returns_none(self):
        assert _finditem("scalar", "a") is None


class TestIsMatch:
    def test_equality_match(self):
        assert is_match({"step_type": "clean"}, step_type="clean") is True

    def test_equality_mismatch(self):
        assert is_match({"step_type": "clean"}, step_type="model") is False

    def test_all_criteria_must_match(self):
        meta = {"step_type": "clean", "generation": 2}
        assert is_match(meta, step_type="clean", generation=2) is True
        assert is_match(meta, step_type="clean", generation=3) is False

    def test_callable_predicate(self):
        meta = {"accuracy": 0.9}
        assert is_match(meta, accuracy=lambda v: v > 0.8) is True
        assert is_match(meta, accuracy=lambda v: v > 0.95) is False

    def test_callable_raising_means_no_match(self):
        assert is_match({}, accuracy=lambda v: v > 0.8) is False

    def test_none_value_matches_missing_key(self):
        # Pins a known quirk: absent keys compare equal to None.
        assert is_match({}, parent_id=None) is True
        assert is_match({"parent_id": None}, parent_id=None) is True

    def test_no_criteria_always_matches(self):
        assert is_match({"anything": 1}) is True


class TestProvenance:
    def test_safe_get_user_returns_string(self):
        assert isinstance(safe_get_user(), str)
        assert safe_get_user() != ""

    def test_get_provenance_structure(self):
        prov = get_provenance()
        assert set(prov) == {
            "user", "python_version", "platform",
            "git_commit", "git_dirty", "git_branch",
        }
        assert isinstance(prov["user"], str)
        assert isinstance(prov["python_version"], str)
        assert isinstance(prov["git_dirty"], bool)

    def test_get_provenance_outside_git_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prov = get_provenance()
        assert prov["git_commit"] is None
        assert prov["git_branch"] is None
        assert prov["git_dirty"] is False


class TestTimeParsing:
    def test_parse_iso_utc_roundtrip(self):
        stamp = "2026-06-10T12:30:45+00:00"
        parsed = parse_iso_utc(stamp)
        assert isinstance(parsed, datetime)
        assert parsed.isoformat() == stamp

    def test_parse_time_formats_iso_string(self):
        assert parse_time("2026-01-02T03:04:05+00:00") == "02 Jan 2026, 03:04:05"

    def test_parse_time_empty_returns_na(self):
        assert parse_time(None) == "N/A"
        assert parse_time("") == "N/A"

    def test_parse_time_garbage_is_returned_unchanged(self):
        assert parse_time("not-a-date") == "not-a-date"
