"""Ingest layer — the write path: scratch -> chunks -> SQLite.

NodeWorkspace (workspace.py, the only filesystem writer), content-defined
chunking + dedup (cdc.py), and the packing pipeline (packing.py).

See REBUILD_BLUEPRINT.md section 5.3.
"""
