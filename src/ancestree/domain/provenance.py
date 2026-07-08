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
from typing import Any, Dict, Optional


def _safe_user() -> str:
    """The current username, degrading gracefully in containers/daemons
    where ``getpass`` has no login to report."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")


def _git_output(*args: str) -> Optional[str]:
    """Runs a git query, returning None when git is absent or the working
    directory is not a repository."""
    try:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, encoding="utf-8"
        ).strip()
    except Exception:
        return None


def capture() -> Dict[str, Any]:
    """The full provenance record for a node being created.

    Returns:
        Dict[str, Any]: ``user``, ``python_version``, ``platform``,
            ``git_commit``, ``git_dirty``, ``git_branch``. The git fields
            are None (and ``git_dirty`` False) outside a repository.
    """
    status = _git_output("status", "--porcelain")
    return {
        "user": _safe_user(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_dirty": bool(status),
        "git_branch": _git_output("rev-parse", "--abbrev-ref", "HEAD"),
    }
