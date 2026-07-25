"""NodeWorkspace — the only module that writes the filesystem.

A node's artifacts are written by the user's own code (pandas, PIL,
``open``), which needs real paths — so each node gets a transient scratch
directory under ``<root>/.scratch/<node_id>/`` for the duration of its
``create_node`` block. On clean ingest the scratch is deleted; nothing
durable ever lives here.

A tiny **seed file** (node_id, step_type, parents, pid, start time) is
written at block start so a hard-killed block's scratch can be adopted as
an unhealthy node by the startup sweep (Phase 5) — partial work stays
evidence even across a SIGKILL.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 3, issue #14).
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The crash-recovery seed, written at block start. Hidden and reserved:
#: never listed as an artifact, never writable through resolve().
SEED_FILENAME = ".ancestree-seed.json"

#: Prefix for the staging directory a node is assembled in before being
#: renamed into place. Dot-prefixed so the orphan sweep skips it: a staging
#: directory is by definition not yet a node.
STAGING_PREFIX = ".staging-"


class NodeWorkspace:
    """The scratch directory one node writes its artifacts into."""

    def __init__(
        self,
        root: str | Path,
        node_id: str,
        step_type: str,
        parent_ids: Sequence[str] = (),
        generation: int = 0,
    ) -> None:
        self.root = Path(root)
        self.node_id = node_id
        scratch_root = self.root / ".scratch"
        self.path = scratch_root / node_id
        seed: dict[str, Any] = {
            "node_id": node_id,
            "step_type": step_type,
            "parent_ids": list(parent_ids),
            "generation": generation,
            "pid": os.getpid(),
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        seed_json = json.dumps(seed, indent=2)

        if self.path.exists():
            # Adopting a scratch directory that already holds a crashed
            # session's work (maintenance.sweep_orphan_scratch): re-seed it
            # in place, because its files ARE the evidence.
            (self.path / SEED_FILENAME).write_text(seed_json)
            return

        # A new node is assembled in a staging directory and renamed into
        # place once it is seeded, so it is never observable as a
        # directory-without-a-seed. That state is what the orphan sweep
        # deletes as litter, and a concurrent store open used to be able to
        # delete an in-flight node's scratch out from under it.
        scratch_root.mkdir(parents=True, exist_ok=True)
        staging = scratch_root / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
        staging.mkdir()
        (staging / SEED_FILENAME).write_text(seed_json)
        try:
            staging.rename(self.path)
        except OSError:
            # The target appeared underneath us. Whatever is there belongs
            # to this node_id, so adopt it exactly as the branch above does
            # rather than leave work stranded under the staging name.
            shutil.rmtree(staging, ignore_errors=True)
            self.path.mkdir(parents=True, exist_ok=True)
            (self.path / SEED_FILENAME).write_text(seed_json)

    def resolve(self, relative: str | Path) -> Path:
        """A ready-to-write path inside the scratch dir — the engine behind
        the node's ``/`` operator. Intermediate directories are created, so
        the returned path can be written to immediately.

        Raises:
            ValueError: If the path escapes the scratch directory or names
                the reserved seed file.
        """
        target = (self.path / relative).resolve()
        if not target.is_relative_to(self.path.resolve()):
            raise ValueError(
                f"Artifact path {str(relative)!r} escapes the node's "
                "directory. Keep all artifact paths inside the node."
            )
        if target.name == SEED_FILENAME:
            raise ValueError(
                f"{SEED_FILENAME!r} is reserved for ancestree's crash "
                "recovery and cannot be written as an artifact."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def files(self) -> list[tuple[str, Path]]:
        """Every artifact file written so far, as (relpath, absolute path)
        pairs sorted by relpath. The seed file is excluded — it describes
        the node, it is not its content."""
        found: list[tuple[str, Path]] = []
        for path in self.path.rglob("*"):
            if not path.is_file() or path.name == SEED_FILENAME:
                continue
            found.append((path.relative_to(self.path).as_posix(), path))
        return sorted(found)

    def discard(self) -> None:
        """Deletes the scratch directory. Called after a successful ingest
        (the bytes are durable in SQLite) or when an untouched node is
        abandoned."""
        shutil.rmtree(self.path, ignore_errors=True)
