"""Phase 9: the CLI — export and compact subcommands (issue #20)."""

import json
from pathlib import Path

import pytest

from ancestree.__main__ import main
from ancestree.store import LineageStore


def _build_store(root: Path) -> str:
    store = LineageStore(root)
    with store.create_node(step_type="ingest") as node:
        (node / "raw.csv").write_text("a,b\n1,2\n")
        node.add_meta("rows", 1)
    node_id = node.node_id
    store.close()
    return node_id


def test_cli_export_writes_sidecars(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "proj"
    node_id = _build_store(root)

    assert main(["export", str(root)]) == 0
    out = capsys.readouterr().out
    assert "wrote sidecars for 1 node(s)" in out

    document = json.loads((root / "export" / node_id / "meta.json").read_text())
    assert document["step_type"] == "ingest"


def test_cli_export_custom_dest(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    node_id = _build_store(root)
    dest = tmp_path / "sidecars"
    assert main(["export", str(root), "--dest", str(dest)]) == 0
    assert (dest / node_id / "meta.json").exists()


def test_cli_compact_reports_reclaimed_chunks(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "proj"
    node_id = _build_store(root)
    store = LineageStore(root)
    # compact=False leaves the chunks orphaned for the CLI to reclaim.
    store.prune(node_id, dry_run=False, compact=False)
    store.close()

    assert main(["compact", str(root)]) == 0
    out = capsys.readouterr().out
    assert "removed 1 unreferenced chunk(s)" in out


def test_cli_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        main([])