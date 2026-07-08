"""Top-level package for ancestree."""

from importlib.metadata import (
    PackageNotFoundError as _PackageNotFoundError,
    version as _version,
)

from .domain.node import Node
from .store import LineageStore

__author__ = """Joshua Smith"""
__email__ = "78921007+JS195@users.noreply.github.com"

try:
    __version__ = _version("ancestree-track")
except _PackageNotFoundError:
    __version__ = "unknown"

__all__ = ["LineageStore", "Node"]
