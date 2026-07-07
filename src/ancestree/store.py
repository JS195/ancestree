"""LineageStore — the public facade.

Wires ConnectionManager, MetadataStore, ChunkStore, RuleEngine and Pruner, and
exposes the public API: create_node, get/find/latest/lineage/children/
ancestors, prune, compact, sql, stats, export, generate_web_graph and
host_live_graph. Orchestration only — every algorithm lives in a focused
module.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 4 (issue #15).
"""
