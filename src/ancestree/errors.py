"""Typed exception hierarchy.

AncestreeError base plus InvalidTransition, NodeNotFound, SchemaError,
IntegrityError and CorruptChunkError, so callers can catch selectively.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 2 (issue #13).
"""
