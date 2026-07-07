"""Node content identity: content_hash and content_equal.

Backs node-level deduplication: the hash is a fast bucket key; equality is the
byte-verified check before an existing node is reused.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 5 (issue #16).
"""
