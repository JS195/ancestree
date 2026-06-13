"""Top-level package for ancestree."""

from importlib.metadata import version, PackageNotFoundError

__author__ = """Joshua Smith"""
__email__ = "josh.smith195@outlook.com"

try:
    __version__ = version("ancestree")
except PackageNotFoundError:
    __version__ = "unknown"

# Clean up the packaging tools so they don't leak into the public namespace 
del version, PackageNotFoundError

from .core import LineageStore

__all__ = ["LineageStore"]