"""Shared fixtures for the ancestree test suite.

All stores are rooted under pytest's tmp_path so nothing touches the real
filesystem. Where a test needs a specific internal state (shelf contents,
config file, broken meta.json) it builds that state directly rather than
going through the full public API.
"""

import time

import pytest

from ancestree import LineageStore

RULES = {"ingest": [None], "clean": ["ingest"], "model": ["clean"]}
TRIGGERS = ["clean"]


def _make_node(store, step_type, parent=None, files=("data.csv",), meta=None):
    """Create and persist a node with the given artifact files and metadata."""
    with store.create_node(step_type=step_type, parent=parent) as node:
        for fname in files:
            (node / fname).write_text(f"contents of {fname}")
        for key, value in (meta or {}).items():
            node.add_meta(key, value)
    # Guarantee strictly increasing timestamps for "most recent" queries.
    time.sleep(0.002)
    return node


@pytest.fixture
def make_node():
    return _make_node


@pytest.fixture
def bare_store(tmp_path):
    """A freshly initialised store with rules and triggers but no nodes."""
    return LineageStore(root=tmp_path / "store", rules=RULES, gen_triggers=TRIGGERS)


@pytest.fixture
def chain_store(bare_store):
    """A store with a linear chain: ingest -> clean -> model."""
    ingest = _make_node(bare_store, "ingest", meta={"source": "api"})
    clean = _make_node(bare_store, "clean", parent=ingest, files=("clean.csv",))
    model = _make_node(
        bare_store, "model", parent=clean, files=("model.pkl",), meta={"accuracy": 0.9}
    )
    return bare_store, {"ingest": ingest, "clean": clean, "model": model}


@pytest.fixture
def branch_store(bare_store):
    """A store with a branching lineage:

    root(ingest) -> left(clean)  -> leaf(model)
                 -> right(clean)
    """
    root = _make_node(bare_store, "ingest")
    left = _make_node(bare_store, "clean", parent=root, files=("left.csv",))
    right = _make_node(bare_store, "clean", parent=root, files=("right.csv",))
    leaf = _make_node(bare_store, "model", parent=left, files=("m.pkl",))
    return bare_store, {"root": root, "left": left, "right": right, "leaf": leaf}
