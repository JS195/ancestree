"""RuleEngine — transition validation and generation numbering.

``validate(step_type, parent_step_types)`` raises InvalidTransition;
``generation_for(step_type, parents)`` computes the node generation.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 4 (issue #15).
"""
