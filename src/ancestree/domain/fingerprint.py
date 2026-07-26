"""Node content identity: the fingerprint behind reuse_identical.

A node's content is its step_type, its parents (order-independent), its
user metadata envelopes and its artifact digests — never the volatile
fields (node_id, timestamps, duration, size, healthy flag) or provenance,
so two runs producing the same step with the same metadata and identical
artifact bytes share a fingerprint.

The digest is only a fast bucket key: the store verifies a candidate by
comparing full ContentSummary values (artifact equality via per-file
SHA-256) before reusing a node, so a fingerprint collision can never merge
two genuinely different nodes.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 5, issue #16).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContentSummary:
    """Everything that makes up a node's content identity.

    Compare instances with ``==`` — the dict fields make them unhashable,
    which is fine: the *digest* is the hashable key.
    """

    step_type: str
    parent_ids: tuple[str, ...]  # sorted: parent order is not content
    metadata: dict[str, dict[str, Any]]
    artifact_sha256s: dict[str, str]

    @classmethod
    def of(
        cls,
        step_type: str,
        parent_ids: Iterable[str],
        metadata: Mapping[str, dict[str, Any]],
        artifact_sha256s: Mapping[str, str],
    ) -> ContentSummary:
        """Builds a summary with the parents normalised to a sorted tuple,
        so `["a", "b"]` and `["b", "a"]` fingerprint identically."""
        return cls(
            step_type=step_type,
            parent_ids=tuple(sorted(parent_ids)),
            metadata=dict(metadata),
            artifact_sha256s=dict(artifact_sha256s),
        )

    @property
    def digest(self) -> str:
        """The SHA-256 fingerprint stored in the node table's content_hash
        column and used as the content-identity bucket key."""
        payload = {
            "step_type": self.step_type,
            "parents": list(self.parent_ids),
            "meta": self.metadata,
            "artifacts": self.artifact_sha256s,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
