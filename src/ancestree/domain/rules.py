"""RuleEngine — transition validation and generation numbering.

The pipeline's grammar, lifted out of ``create_node`` into pure,
unit-testable logic: which step types may follow which, and how a node's
generation derives from its parents.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 4, issue #15).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..errors import InvalidTransition

# step_type is a category key (rules, the web graph, search), so it must be
# a short, printable label — not empty, not a wall of text, no control
# characters.
_MAX_STEP_TYPE_LEN = 100


def validate_step_type(step_type: str) -> None:
    """Rejects a step_type that is empty, over-long, or holds control
    characters. Arbitrary printable text (any language, symbols) is fine.

    Raises:
        ValueError: If the label is unusable as a category key.
    """
    if not isinstance(step_type, str) or not step_type.strip():
        raise ValueError("step_type must be a non-empty string.")
    if len(step_type) > _MAX_STEP_TYPE_LEN:
        raise ValueError(
            f"step_type is too long ({len(step_type)} characters; max "
            f"{_MAX_STEP_TYPE_LEN}). Use a short label for the kind of step."
        )
    if not step_type.isprintable():
        raise ValueError(
            "step_type must contain only printable characters "
            "(no newlines, tabs, or other control characters)."
        )


class RuleEngine:
    """Enforces the store's transition rules and generation numbering."""

    def __init__(
        self,
        rules: Mapping[str, Sequence[str]] | None,
        gen_triggers: Sequence[str] | None,
    ) -> None:
        self.rules = {key: list(value) for key, value in (rules or {}).items()}
        self.gen_triggers = list(gen_triggers or [])

    def validate(self, step_type: str, parent_step_types: Sequence[str | None]) -> None:
        """Checks that every parent is a legal predecessor of `step_type`.

        A step type without a rules entry accepts anything. A root node is
        checked as having the single parent type None — so a rules-listed
        step can only be a root if its allowed list contains None.

        Raises:
            InvalidTransition: If any parent (or rootness) is not allowed.
        """
        allowed = self.rules.get(step_type)
        if allowed is None:
            return
        for parent_type in list(parent_step_types) or [None]:
            if parent_type not in allowed:
                raise InvalidTransition(
                    f"Invalid transition: {parent_type} -> {step_type}. "
                    f"Allowed parents: {allowed}."
                )

    def generation_for(self, step_type: str, parent_generations: Sequence[int]) -> int:
        """A node sits one generation past its deepest parent when its step
        type triggers a new generation (and it has parents at all);
        otherwise it stays at that depth. Roots are generation 0."""
        base = max(parent_generations, default=0)
        if parent_generations and step_type in self.gen_triggers:
            return base + 1
        return base
