"""Typed exception hierarchy.

Every error ancestree raises deliberately derives from AncestreeError, so
callers can catch selectively instead of fishing bare ValueError/RuntimeError
out of library code (the 0.1.x pattern).

See REBUILD_BLUEPRINT.md section 5.3 (pulled forward from Phase 2 into
Phase 1: SchemaError backs schema verification).
"""


class AncestreeError(Exception):
    """Base class for every deliberate ancestree error."""


class InvalidTransition(AncestreeError, ValueError):
    """A step_type is not allowed to follow its parents under the store's
    rules. Also a ValueError, matching what 0.1.x raised here."""


class NodeNotFound(AncestreeError):
    """A node_id does not exist in this store."""


class SchemaError(AncestreeError):
    """The database file is not a usable ancestree store: a foreign SQLite
    file, a format newer than this package, or missing tables."""


class IntegrityError(AncestreeError):
    """Stored state fails an internal consistency check."""


class CorruptChunkError(IntegrityError):
    """A chunk's reassembled bytes no longer match their recorded digest."""
