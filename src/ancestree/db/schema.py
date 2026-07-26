"""The canonical DDL.

``SCHEMA_SQL`` is the one authoritative statement of the store's shape —
REBUILD_BLUEPRINT.md section 6 mirrors it. ``ensure_schema`` creates a fresh
database atomically or verifies an existing one. Database-level settings
that must be chosen at creation time (``auto_vacuum``, ``journal_mode``) are
set here, once; per-connection pragmas live in connection.py.

See REBUILD_BLUEPRINT.md sections 5.3 and 6 (Phase 1, issue #12).
"""

from __future__ import annotations

import sqlite3

from ..errors import SchemaError

#: Every table the schema defines; an opened store is verified against this.
TABLES: frozenset[str] = frozenset(
    {
        "config",
        "node",
        "edge",
        "metadata",
        "chunk",
        "chunk_feature",
        "artifact",
        "artifact_chunk",
    }
)

SCHEMA_SQL = """
-- Store-wide configuration: rules, gen_triggers, dedup/chunk policy, format.
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                        -- JSON
);

-- One row per node. Structural + provenance facts are real columns.
CREATE TABLE node (
    node_id         TEXT PRIMARY KEY,          -- 8-char id
    step_type       TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    created_utc     TEXT NOT NULL,             -- ISO-8601
    created_epoch   REAL NOT NULL,             -- latest() / colour-by-time
    healthy         INTEGER NOT NULL,          -- 0/1
    duration_s      REAL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT,                      -- node-level dedup bucket key
    prov_user       TEXT,
    prov_python     TEXT,
    prov_platform   TEXT,
    prov_git_commit TEXT,
    prov_git_branch TEXT,
    prov_git_dirty  INTEGER
);
CREATE INDEX idx_node_step ON node(step_type);
CREATE INDEX idx_node_hash ON node(content_hash);
CREATE INDEX idx_node_gen  ON node(generation);

-- DAG edges: a node may have several parents (a join). Cascades on delete.
CREATE TABLE edge (
    child_id  TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    parent_id TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL,                -- preserves parent order
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX idx_edge_parent ON edge(parent_id);

-- User metadata only. The envelope is preserved; value is JSON so it holds
-- scalars, lists, dicts, or table blobs. num_value backs numeric ranges.
CREATE TABLE metadata (
    node_id    TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT,                           -- JSON-encoded
    data_type  TEXT NOT NULL,                  -- text|image|link|table|json|code
    grp        TEXT,
    searchable INTEGER NOT NULL,               -- 0/1
    num_value  REAL,                           -- extracted numeric value
    PRIMARY KEY (node_id, key)
);
-- (key, value) not (key): an equality find is then a covering index seek
-- rather than a scan of every row sharing the key. On 5k nodes that is
-- 0.67 ms -> 0.01 ms for a selective lookup, for ~2% more file.
CREATE INDEX idx_meta_key ON metadata(key, value) WHERE searchable = 1;
CREATE INDEX idx_meta_num ON metadata(key, num_value) WHERE num_value IS NOT NULL;

-- Content-addressed chunk pool. Each chunk stored once (INSERT OR IGNORE is
-- the dedup). A chunk is raw (zlib), stored (verbatim, when zlib made it no
-- smaller), or a delta: zlib dictionary-compressed against base_digest.
-- Depth is capped at 1: a base is never itself a delta.
CREATE TABLE chunk (
    digest        TEXT PRIMARY KEY,            -- sha256 of the PLAINTEXT chunk
    kind          INTEGER NOT NULL,            -- 0 raw(zlib) 1 delta(zdict) 2 verbatim
    base_digest   TEXT REFERENCES chunk(digest),
    data          BLOB NOT NULL,               -- zlib(raw) or the delta stream
    length        INTEGER NOT NULL,            -- plaintext length
    created_epoch REAL NOT NULL
);

-- Resemblance index: super-feature -> chunk, for near-duplicate discovery.
CREATE TABLE chunk_feature (
    feature INTEGER NOT NULL,
    digest  TEXT NOT NULL REFERENCES chunk(digest) ON DELETE CASCADE,
    PRIMARY KEY (feature, digest)
);
CREATE INDEX idx_feature ON chunk_feature(feature);

-- One logical output file of a node.
CREATE TABLE artifact (
    node_id TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,                     -- path relative to the node
    size    INTEGER NOT NULL,
    sha256  TEXT NOT NULL,                     -- whole-artifact digest
    PRIMARY KEY (node_id, relpath)
);

-- Ordered recipe: which chunks reconstruct each artifact.
CREATE TABLE artifact_chunk (
    node_id TEXT NOT NULL,
    relpath TEXT NOT NULL,
    ordinal INTEGER NOT NULL,                  -- chunk order within the artifact
    digest  TEXT NOT NULL REFERENCES chunk(digest),
    PRIMARY KEY (node_id, relpath, ordinal),
    FOREIGN KEY (node_id, relpath)
        REFERENCES artifact(node_id, relpath) ON DELETE CASCADE
);
CREATE INDEX idx_ac_digest ON artifact_chunk(digest);
"""


def _tables(conn: sqlite3.Connection) -> set[str]:
    """The user tables present in the database (SQLite internals excluded)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Creates the schema on a fresh database, or verifies an existing one.

    A fresh (table-less) database gets the creation-time settings and the
    full DDL in one atomic script — either the whole schema lands or none of
    it. An existing store is checked for the tables it should have.

    Args:
        conn: An open connection to the store's database file.

    Raises:
        SchemaError: If the file holds tables but is missing expected ones —
            either not an ancestree store, or a corrupt one.
    """
    existing = _tables(conn)
    if not existing:
        # Creation-time, database-level settings. auto_vacuum only takes
        # effect if set before the first table exists; journal_mode=WAL is
        # persistent, so every later connection inherits it.
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(f"BEGIN;\n{SCHEMA_SQL}\nCOMMIT;")
        return

    missing = TABLES - existing
    if missing:
        raise SchemaError(
            f"Store is missing expected tables {sorted(missing)}; this "
            "SQLite file was not written by ancestree, or is corrupt."
        )
