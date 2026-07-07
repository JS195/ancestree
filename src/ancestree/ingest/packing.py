"""The ingest pipeline: scratch files -> chunks -> SQLite, in one transaction.

Chunks each written file, deduplicates via ChunkStore, and computes artifact
digests, node size and content_hash in the same pass. Synchronous at block
exit by default (AD4).

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 3 (issue #14).
"""
