"""Top-level package for ancestree."""

from importlib.metadata import version as _version, PackageNotFoundError as _PackageNotFoundError
from .core import LineageStore

__author__ = """Joshua Smith"""
__email__ = "josh.smith195@outlook.com"

try:
    __version__ = _version("ancestree")
except _PackageNotFoundError:
    __version__ = "unknown"

__all__ = ["LineageStore"]