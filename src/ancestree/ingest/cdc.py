"""Content-defined chunking and advanced dedup, in one sectioned module.

(1) FastCDC chunker — Gear rolling hash, normalized masks, fixed-boundary
large-file fallback (AD4); (2) resemblance — super-features computed in the
chunking pass (Layer 2 discovery); (3) delta codec — zlib dictionary
compression against a raw base chunk (AD5).

See REBUILD_BLUEPRINT.md section 5.3. The chunker arrives in Phase 3
(issue #14); resemblance and delta arrive in Phase 6 (issue #17).
"""
