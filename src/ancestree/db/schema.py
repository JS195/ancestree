"""The canonical DDL, schema versioning and migrations.

``SCHEMA_SQL`` is the one authoritative statement of the store's shape —
REBUILD_BLUEPRINT.md section 6 mirrors it. ``ensure_schema`` creates a fresh
database atomically (stamping ``PRAGMA user_version``) or verifies an
existing one; ``migrate`` steps an older store forward one version at a
time. Database-level settings that must be chosen at creation time
(``auto_vacuum``, ``journal_mode``) are set here, once; per-connection
pragmas live in connection.py.

See REBUILD_BLUEPRINT.md sections 5.3 and 6 (Phase 1, issue #12).
"""

from __future__ import annotations

import sqlite3
from typing import Dict, FrozenSet, Set

from ..errors import SchemaError

#: Stamped into ``PRAGMA user_version``. Bump alongside a _MIGRATIONS entry.
SCHEMA_VERSION = 2

#: Every table the schema defines; an opened store is verified against this.
TABLES: FrozenSet[str] = frozenset(
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

#: from_version -> SQL script upgrading the store to from_version + 1.
_MIGRATIONS: Dict[int, str] = {
    # v1 -> v2. Two changes, one of which is pure DDL:
    #
    # 1. The metadata equality index gains its value column (rebuilt here).
    # 2. The chunk *encoding* changed: a smaller average chunk size, and
    #    kind 2 (stored verbatim) for payloads zlib cannot shrink. Every v1
    #    chunk still decodes unchanged, so an existing store opens and reads
    #    normally; only newly written artifacts use the new boundaries, and
    #    they will not deduplicate against v1-era chunks of the same
    #    content. The stamp exists so a v1 ancestree refuses a v2 store up
    #    front instead of failing on an unknown chunk kind.
    1: (
        "DROP INDEX IF EXISTS idx_meta_key;\n"
        "CREATE INDEX idx_meta_key ON metadata(key, value) "
        "WHERE searchable = 1;"
    ),
}


def _tables(conn: sqlite3.Connection) -> Set[str]:
    """The user tables present in the database (SQLite internals excluded)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def schema_version(conn: sqlite3.Connection) -> int:
    """The store's ``PRAGMA user_version`` (0 on a file ancestree never
    stamped)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Creates the schema on a fresh database, or verifies an existing one.

    A fresh (table-less) database gets the creation-time settings, the full
    DDL and the version stamp in one atomic script — either the whole schema
    lands or none of it. An existing store is version-checked, migrated
    forward if older, and verified to hold every expected table.

    Args:
        conn: An open connection to the store's database file.

    Raises:
        SchemaError: If the file holds tables but no ancestree version stamp
            (not an ancestree store), was created by a newer ancestree, has
            no migration path, or is missing expected tables.
    """
    existing = _tables(conn)
    if not existing:
        # Creation-time, database-level settings. auto_vacuum only takes
        # effect if set before the first table exists; journal_mode=WAL is
        # persistent, so every later connection inherits it.
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            f"BEGIN;\n{SCHEMA_SQL}\n"
            f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
        )
        return

    version = schema_version(conn)
    if version == 0:
        raise SchemaError(
            "This SQLite file was not created by ancestree (it holds tables "
            "but no ancestree schema version)."
        )
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"This store uses schema v{version}, which is newer than this "
            f"ancestree (v{SCHEMA_VERSION}). Upgrade the package to open it."
        )
    if version < SCHEMA_VERSION:
        migrate(conn, version)
        existing = _tables(conn)

    missing = TABLES - existing
    if missing:
        raise SchemaError(
            f"Store is missing expected tables {sorted(missing)}; the "
            "database may be corrupt."
        )


def migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Steps an older store forward to SCHEMA_VERSION, one migration at a
    time, stamping ``user_version`` after each so an interrupted upgrade
    resumes where it stopped.

    A stub today: v1 is the first SQLite format, so no migrations exist yet.
    A future format change adds an entry to ``_MIGRATIONS`` and bumps
    ``SCHEMA_VERSION``.

    Raises:
        SchemaError: If a required migration step is not defined.
    """
    for version in range(from_version, SCHEMA_VERSION):
        script = _MIGRATIONS.get(version)
        if script is None:
            raise SchemaError(
                f"No migration path from schema v{version}; this store "
                "cannot be opened by this version of ancestree."
            )
        conn.executescript(
            f"BEGIN;\n{script}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
        )
