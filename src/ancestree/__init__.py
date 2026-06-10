"""Top-level package for ancestree."""
from importlib.metadata import version, PackageNotFoundError

__author__ = """Joshua Smith"""
__email__ = "josh.smith195@outlook.com"

try:
    __version__ = version("ancestree")
except PackageNotFoundError:
    __version__ = "unknown"

from .core import LineageStore
from .models import Node
from .utils import format_metadata

__all__ = ["LineageStore", "Node", "format_metadata"]