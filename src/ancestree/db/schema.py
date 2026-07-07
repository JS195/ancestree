"""The canonical DDL, schema versioning and migrations.

SCHEMA_SQL and SCHEMA_VERSION (PRAGMA user_version); ensure_schema() for fresh
databases; migrate() steps versions forward. auto_vacuum=INCREMENTAL is set at
creation time.

See REBUILD_BLUEPRINT.md sections 5.3 and 6. Arrives in Phase 1 (issue #12).
"""
