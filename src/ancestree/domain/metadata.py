"""The metadata envelope: validation, coercion and type inference.

Builds validated ``{value, data_type, group, searchable}`` entries (reserved-
key guard, ``auto`` type inference, DataFrame/json coercion) and extracts
``num_value`` for the numeric index.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 4 (issue #15).
"""
