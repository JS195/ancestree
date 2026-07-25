"""Phase 0 exit criterion: every rebuild-skeleton module imports.

The skeleton (REBUILD_BLUEPRINT.md section 5.1) grows alongside the 0.1.x
modules until Phase 9; this test pins the exit criterion for issue #11.
"""

import importlib

import pytest

SKELETON_MODULES = [
    "ancestree.__main__",
    "ancestree.db",
    "ancestree.db.chunk_store",
    "ancestree.db.connection",
    "ancestree.db.metadata_store",
    "ancestree.db.schema",
    "ancestree.domain",
    "ancestree.domain.fingerprint",
    "ancestree.domain.metadata",
    "ancestree.domain.node",
    "ancestree.domain.provenance",
    "ancestree.domain.rules",
    "ancestree.errors",
    "ancestree.ingest",
    "ancestree.ingest.cdc",
    "ancestree.ingest.packing",
    "ancestree.ingest.workspace",
    "ancestree.maintenance",
    "ancestree.store",
    "ancestree.util",
    "ancestree.web",
    "ancestree.web.export",
    "ancestree.web.graph",
    "ancestree.web.server",
]


@pytest.mark.parametrize("module_name", SKELETON_MODULES)
def test_skeleton_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
