"""Provenance capture: who/what/how produced a node.

``capture()`` returns the reproducibility context recorded on every node:
user, Python version, platform, git commit/branch, and the dirty-worktree
flag. Every lookup is best-effort — a machine without git, or a run outside
a repository, must never break node creation.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 2, issue #13).
"""

from __future__ import annotations

import getpass
import os
import platform
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def _safe_user() -> str:
    """The current username, degrading gracefully in containers/daemons
    where ``getpass`` has no login to report."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - any failure means 'no login to report'
        return os.environ.get("USER", "unknown")


def _git_output(*args: str) -> str | None:
    """Runs a git query, returning None when git is absent or the working
    directory is not a repository."""
    try:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, encoding="utf-8"
        ).strip()
    except Exception:  # noqa: BLE001 - git absent, not a repo, or any other failure
        return None


def capture() -> dict[str, Any]:
    """The full provenance record for a node being created.

    Runs on every ``create_node``, and spawning git is by far the most
    expensive thing node creation does — so the two queries are issued
    concurrently, and the commit and branch come back from one invocation
    (``rev-parse`` accepts both in a single call). Nothing is cached: the
    worktree can be edited, committed or checked out between two nodes in
    the same session, and a stale ``git_dirty`` would quietly misreport
    whether a result is reproducible.

    Returns:
        Dict[str, Any]: ``user``, ``python_version``, ``platform``,
            ``git_commit``, ``git_dirty``, ``git_branch``. The git fields
            are None (and ``git_dirty`` False) outside a repository.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        status_result = pool.submit(_git_output, "status", "--porcelain")
        revs_result = pool.submit(
            _git_output, "rev-parse", "HEAD", "--abbrev-ref", "HEAD"
        )
        status = status_result.result()
        revs = revs_result.result()

    # "<commit>\n<branch>" in a repo with a resolvable HEAD; None otherwise
    # (no git, not a repo, or a fresh repo with no commit yet).
    lines = revs.splitlines() if revs else []
    return {
        "user": _safe_user(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": lines[0] if len(lines) > 0 else None,
        "git_dirty": bool(status),
        "git_branch": lines[1] if len(lines) > 1 else None,
    }
