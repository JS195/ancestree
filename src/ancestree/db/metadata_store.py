"""MetadataStore — node/edge/metadata rows and every metadata query.

add_node in one transaction; find (multi-key INTERSECT); lineage (recursive
CTE); children; most_recent; find_by_hash; remove.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 2 (issue #13).
"""
