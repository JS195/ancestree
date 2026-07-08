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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple, Union

#: The crash-recovery seed, written at block start. Hidden and reserved:
#: never listed as an artifact, never writable through resolve().
SEED_FILENAME = ".ancestree-seed.json"


class NodeWorkspace:
    """The scratch directory one node writes its artifacts into."""

    def __init__(
        self,
        root: Union[str, Path],
        node_id: str,
        step_type: str,
        parent_ids: Sequence[str] = (),
    ) -> None:
        self.root = Path(root)
        self.node_id = node_id
        self.path = self.root / ".scratch" / node_id
        self.path.mkdir(parents=True, exist_ok=True)
        seed: Dict[str, Any] = {
            "node_id": node_id,
            "step_type": step_type,
            "parent_ids": list(parent_ids),
            "pid": os.getpid(),
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        (self.path / SEED_FILENAME).write_text(json.dumps(seed, indent=2))

    def resolve(self, relative: Union[str, Path]) -> Path:
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

    def files(self) -> List[Tuple[str, Path]]:
        """Every artifact file written so far, as (relpath, absolute path)
        pairs sorted by relpath. The seed file is excluded — it describes
        the node, it is not its content."""
        found: List[Tuple[str, Path]] = []
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
