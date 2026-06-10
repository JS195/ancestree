from datetime import datetime
from typing import Literal, Dict, Any
from pathlib import Path
import re
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

# This should have more type checking
def format_metadata(data_type: Literal['text', 'table', 'link', 'list', 'image'], content: Any, label = None) -> Dict:
    """
    Function to format items in the metadata into something the JS web app can interpret and display.
    If data_type = 'table', content must be a pandas DataFrame.
    If data_type = 'image', content must be a str or Path to a .png or .jpeg file.

    Args:
        data_type: Must be one of 'text', 'table', 'link', 'list', 'image'.
        content: The content to store in the metadata.
        label: The label of the content. Defaults to None.

    Returns:
        Dict: A dictionary of the formatted metadata.

    Examples:
        >>> ancestree.format_metadata("text", "Task complete", label="Status")
        {'type': data_type, 'content': 'Task complete', 'label': 'Status'}
    """
    data_type = data_type.lower()    
    formatted_content = content
    
    if data_type == "table":
        if is_pandas(content):

            split = content.to_dict(orient='split')
            formatted_content = {
                "columns":split['columns'],
                "rows":split['data']
            }

        else:         
            raise TypeError(f"Expected a pandas DataFrame for 'table', got {type(content).__name__}")
    
    if data_type == 'image':
        p = Path(content)

        parts = p.parts
        for i, part in enumerate(parts):
            if re.match(r'^[0-9a-f]{8}$', part):
                formatted_content = str(Path(*parts[i:]))
        
    return {
        "type": data_type, # "text", "table", "link", "list", "image"
        "content": formatted_content,
        "label": label
    }

def _finditem(obj, target_key):
    """Internal helper — flat dict lookup with list support."""
    if isinstance(obj, dict):
        return obj.get(target_key)
    if isinstance(obj, list):
        for item in obj:
            result = _finditem(item, target_key)
            if result is not None:
                return result
    return None

def is_match(meta, **kwargs):
    for key, value in kwargs.items():
        nested_val = _finditem(meta, key)
        if callable(value):
            try:
                if not value(nested_val):
                    return False
            except Exception:
                return False
        elif nested_val != value:
            return False
    return True

def safe_get_user():
    """
    Attempt to retrieve the users credentials
    """
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")

def get_provenance():
    """
    Track who/ what/ how produced the node
    """
    prov = {
        "user": safe_get_user(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": None,
        "git_dirty": False,
        "git_branch": None,
    }

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            encoding="utf-8"
        ).strip()
        prov["git_commit"] = commit

        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            encoding="utf-8"
        ).strip()
        prov["git_dirty"] = len(status) > 0

        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            encoding="utf-8"
        ).strip()
        prov["git_branch"] = branch

    except Exception:
        # Not a git repo or git not installed
        pass
    return prov

def parse_iso_utc(s: str) -> datetime:
    """
    Returns a datetime from a string.

    Args:
        s (str): String object representing a datetime.

    Returns:
        datetime: A datetime object.
    """
    return datetime.fromisoformat(s)

def parse_time(iso_str):
    if not iso_str: return "N/A"
    try:
        dt=datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %Y, %H:%M:%S")
    except:
        return iso_str