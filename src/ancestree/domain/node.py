"""Node — the immutable record returned by queries, plus the recording handle.

The Node record exposes identity, metadata, artifacts and read-side ``/``; the
mutable recording handle yielded by ``create_node`` owns write-side ``/`` and
``add_meta``. Splitting them prevents ``add_meta`` on a queried node (AD10).

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 4 (issue #15).
"""
