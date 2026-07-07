"""ChunkStore — chunk and delta BLOBs, artifact recipes, reassembly, gc.

put_chunk (exact dedup -> resemblance/delta or raw); get_chunk (decode against
the raw base, verify SHA-256); artifact manifests; the session read cache in
the system temp dir.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 3 (issue #14).
"""
