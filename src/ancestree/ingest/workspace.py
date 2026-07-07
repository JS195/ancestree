"""NodeWorkspace — the only module that writes the filesystem.

Creates the transient ``.scratch/<node_id>/`` directory, resolves write
targets (with the escape guard), writes the crash-recovery seed file at block
start, enumerates written files, and drives ingest -> delete at block exit
(AD2).

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 3 (issue #14).
"""
