"""Destructive and space operations: prune, compact, orphan-scratch sweep.

Pruner deletes DAG-aware (a child dies only when all its parents die);
compact() removes unreachable chunks then runs incremental_vacuum; the sweep
adopts a dead process seeded scratch as an unhealthy node.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 5 (issue #16).
"""
