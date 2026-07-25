"""Phase 1: schema creation, verification and refusal (issue #12).

Covers the ensure_schema paths: fresh creation (settings + DDL + version
stamp), idempotent reopen, refusal of foreign/newer files, and that the
schema's own guarantees (foreign keys, cascades) hold through the
ConnectionManager.
"""

import sqlite3
from pathlib import Path

import pytest

from ancestree.db import schema
from ancestree.db.connection import ConnectionManager
from ancestree.db.schema import SCHEMA_VERSION, TABLES
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
    assert _pragma(conn, "user_version") == SCHEMA_VERSION
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
    assert _pragma(second.read(), "user_version") == SCHEMA_VERSION
    second.close()


def test_foreign_sqlite_file_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "other.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE junk (x)")
    raw.commit()
    raw.close()
    with pytest.raises(SchemaError):
        ConnectionManager(db)


def test_newer_store_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    ConnectionManager(db).close()
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(SchemaError):
        ConnectionManager(db)


def test_older_store_is_refused_not_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no migration path, by design. A store from an older format
    must be refused with an explanation, never silently converted.

    v1 is the first format, so "older" is reached by advancing what this
    ancestree writes — which is exactly the situation a future bump creates.
    """
    db = tmp_path / "store.db"
    ConnectionManager(db).close()
    monkeypatch.setattr(schema, "SCHEMA_VERSION", SCHEMA_VERSION + 1)

    with pytest.raises(SchemaError, match="does not migrate"):
        ConnectionManager(db)

    # Refused, and left exactly as it was found.
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    raw.close()


def test_no_migration_machinery_is_exposed() -> None:
    """Migration is not a goal; the module must not carry the hooks for it."""
    from ancestree.db import schema

    assert not hasattr(schema, "migrate")
    assert not hasattr(schema, "_MIGRATIONS")


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
