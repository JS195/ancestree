"""Phase 1: schema creation, verification and refusal (issue #12).

Covers the ensure_schema paths: fresh creation (settings + DDL), idempotent
reopen, refusal of files ancestree did not write, and that the schema's own
guarantees (foreign keys, cascades) hold through the ConnectionManager.
"""

import sqlite3
from pathlib import Path

import pytest

from ancestree.db.connection import ConnectionManager
from ancestree.db.schema import TABLES
from ancestree.errors import SchemaError


def _pragma(conn: sqlite3.Connection, name: str) -> object:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def _add_node(conn: sqlite3.Connection, node_id: str) -> None:
    conn.execute(
        "INSERT INTO node (node_id, step_type, generation, created_utc, "
        "created_epoch, healthy) VALUES (?, 'step', 0, 't', 0.0, 1)",
        (node_id,),
    )


def test_fresh_database_gets_full_schema(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "store.db")
    conn = mgr.read()
    names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == set(TABLES)
    assert _pragma(conn, "journal_mode") == "wal"
    assert _pragma(conn, "auto_vacuum") == 2  # INCREMENTAL
    assert _pragma(conn, "foreign_keys") == 1
    mgr.close()


def test_reopening_an_existing_store_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    first = ConnectionManager(db)
    with first.write() as conn:
        conn.execute("INSERT INTO config VALUES ('rules', '{}')")
    first.close()

    second = ConnectionManager(db)
    row = (
        second.read().execute("SELECT value FROM config WHERE key = 'rules'").fetchone()
    )
    assert row is not None and row["value"] == "{}"
    second.close()


def test_foreign_sqlite_file_is_refused(tmp_path: Path) -> None:
    """A SQLite file ancestree did not write holds tables but not the ones
    the schema defines, so it is refused rather than written into."""
    db = tmp_path / "other.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE junk (x)")
    raw.commit()
    raw.close()

    with pytest.raises(SchemaError, match="missing expected tables"):
        ConnectionManager(db)

    # Refused, and left exactly as it was found.
    raw = sqlite3.connect(db)
    names = {
        row[0]
        for row in raw.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    raw.close()
    assert names == {"junk"}


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "store.db")
    with pytest.raises(sqlite3.IntegrityError), mgr.write() as conn:
        conn.execute("INSERT INTO edge VALUES ('nope', 'also-nope', 0)")
    # The failed transaction rolled back cleanly.
    count = mgr.read().execute("SELECT count(*) AS n FROM edge").fetchone()
    assert count["n"] == 0
    mgr.close()


def test_deleting_a_node_cascades(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "store.db")
    with mgr.write() as conn:
        _add_node(conn, "parent")
        _add_node(conn, "child")
        conn.execute("INSERT INTO edge VALUES ('child', 'parent', 0)")
        conn.execute(
            "INSERT INTO metadata VALUES "
            "('parent', 'accuracy', '0.9', 'text', 'General', 1, 0.9)"
        )
    with mgr.write() as conn:
        conn.execute("DELETE FROM node WHERE node_id = 'parent'")

    conn = mgr.read()
    assert conn.execute("SELECT count(*) AS n FROM edge").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM metadata").fetchone()["n"] == 0
    # The child node itself survives; only the link to the parent died.
    assert conn.execute("SELECT count(*) AS n FROM node").fetchone()["n"] == 1
    mgr.close()
