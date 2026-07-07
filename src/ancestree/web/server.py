"""The local live server: http.server on 127.0.0.1 + a SQL-backed JSON API.

/api/graph, /api/node, /api/search, /api/diff, /api/artifact — every endpoint
resolves by database key, never a filesystem path (AD11).

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 8 (issue #19).
"""
