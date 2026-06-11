from datetime import datetime
import subprocess
import platform
import getpass
import sys
import os

def is_pandas(obj):
    """
    Sniffs an object's metadata to determine if it is a pandas DataFrame instance.

    Args:
        obj: Any python object. 

    Returns:
        bool: Returns True if the object is a pandas DataFrame.
    """
    return type(obj).__name__ == 'DataFrame' and hasattr(obj, 'to_dict')


def safe_get_user():
    """
    Attempt to retrieve the users credentials
    """
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")

def get_environment_provenance():
    """
    Track who and what produced the node.
    """
    return {
        "user": safe_get_user(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }

def _git_output(*args):
    try:
        return subprocess.check_output(
            ["git", *args],
            stderr=subprocess.DEVNULL,
            encoding="utf-8"
        ).strip()
    except Exception:
        # Not a git repo or git not installed
        return None

def get_git_provenance():
    """
    Track the git state the node was produced under.
    """
    status = _git_output("status", "--porcelain")
    return {
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_dirty": bool(status),
        "git_branch": _git_output("rev-parse", "--abbrev-ref", "HEAD"),
    }

def get_provenance():
    """
    Track who/ what/ how produced the node. Returns a flat dict so each
    field can be stored as an individual metadata entry via add_meta.
    """
    return {**get_environment_provenance(), **get_git_provenance()}

def parse_time(iso_str):
    if not iso_str: return "N/A"
    try:
        dt=datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %Y, %H:%M:%S")
    except:
        return iso_str