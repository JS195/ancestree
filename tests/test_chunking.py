"""Sub-file deduplication via content-defined chunking (LineageStore(chunk=True)).

With chunking on, each artifact is split into content-defined chunks stored once
in a shared pool (<root>/.chunks) as the node is persisted, and reassembled on
demand when read. Near-identical artifacts across nodes share all but their
differing chunks. Reading is transparent (node / "file", node.artifacts());
space is reclaimed with compact()/gc().

Run with: pytest tests/test_chunking.py
"""

import json
import os
import time

import pytest

import ancestree
from ancestree.chunkstore import ChunkStore, _MAX_SIZE, _MIN_SIZE, chunk_bytes


@pytest.fixture
def chunk_store(tmp_path):
    return ancestree.LineageStore(tmp_path / "store", chunk=True)


def manifest(store, node_id):
    return json.loads((store.root / node_id / ".artifacts.json").read_text())


def pool(store):
    return set(ChunkStore(store.root).all_digests())


def age_chunks(store, secs=120):
    """Backdate every chunk's mtime so gc's grace window no longer protects it."""
    cs = ChunkStore(store.root)
    past = time.time() - secs
    for digest in cs.all_digests():
        os.utime(cs._path(digest), (past, past))


# ---------------------------------------------------------------------------
# The chunker
# ---------------------------------------------------------------------------


class TestChunker:
    def test_chunking_is_deterministic(self):
        data = os.urandom(300_000)
        assert list(chunk_bytes(data)) == list(chunk_bytes(data))

    def test_reassembly_is_lossless(self):
        data = os.urandom(300_000)
        assert b"".join(chunk_bytes(data)) == data

    def test_interior_chunks_respect_bounds(self):
        chunks = list(chunk_bytes(os.urandom(500_000)))
        assert len(chunks) > 1
        for chunk in chunks[:-1]:  # the final chunk may be short
            assert _MIN_SIZE <= len(chunk) <= _MAX_SIZE

    def test_small_file_is_one_chunk(self):
        assert len(list(chunk_bytes(b"tiny"))) == 1

    def test_local_edit_changes_few_chunks(self):
        base = os.urandom(300_000)
        edited = base[:150_000] + b"!!!!" + base[150_004:]  # 4 bytes, same length
        a = list(chunk_bytes(base))
        b = list(chunk_bytes(edited))
        shared = set(a) & set(b)
        # The vast majority of chunks are untouched by a localised edit.
        assert len(shared) >= len(a) - 3


# ---------------------------------------------------------------------------
# Transparent read/write
# ---------------------------------------------------------------------------


class TestTransparency:
    def test_artifact_is_packed_and_file_removed(self, chunk_store):
        with chunk_store.create_node(step_type="ingest") as node:
            (node / "data.bin").write_bytes(os.urandom(50_000))
        assert not (chunk_store.root / node.node_id / "data.bin").exists()
        assert "data.bin" in manifest(chunk_store, node.node_id)
        assert pool(chunk_store)  # chunks were written

    def test_truediv_rehydrates_on_read(self, chunk_store):
        payload = os.urandom(50_000)
        with chunk_store.create_node(step_type="ingest") as node:
            (node / "data.bin").write_bytes(payload)

        reopened = chunk_store.get_node(node.node_id)
        assert (reopened / "data.bin").read_bytes() == payload

    def test_artifacts_lists_and_resolves(self, chunk_store):
        with chunk_store.create_node(step_type="ingest") as node:
            (node / "a.bin").write_bytes(b"x" * 40_000)
            (node / "sub/b.bin").write_bytes(b"y" * 40_000)

        reopened = chunk_store.get_node(node.node_id)
        listed = set(reopened.artifacts())
        assert listed == {reopened.path / "a.bin", reopened.path / "sub/b.bin"}
        for path in listed:
            assert path.exists()  # resolved to real bytes

    def test_from_parent_reads_through_chunks(self, chunk_store):
        with chunk_store.create_node(step_type="ingest") as parent:
            (parent / "raw.bin").write_bytes(b"z" * 40_000)
        with chunk_store.create_node(step_type="clean", parent=parent) as child:
            child.add_meta("note", "derived")
            [raw] = chunk_store.from_parent(child, "raw.bin")
            assert (chunk_store.root / raw).read_bytes() == b"z" * 40_000


# ---------------------------------------------------------------------------
# Sharing across nodes
# ---------------------------------------------------------------------------


class TestSharing:
    def test_near_identical_files_share_chunks(self, chunk_store):
        base = os.urandom(300_000)
        edited = base[:150_000] + b"####" + base[150_004:]
        with chunk_store.create_node(step_type="ingest") as a:
            (a / "f.bin").write_bytes(base)
        with chunk_store.create_node(step_type="ingest") as b:
            (b / "f.bin").write_bytes(edited)

        a_chunks = {c for r in manifest(chunk_store, a.node_id).values() for c in r["chunks"]}
        b_chunks = {c for r in manifest(chunk_store, b.node_id).values() for c in r["chunks"]}
        assert a_chunks & b_chunks  # they share chunks
        # The pool is far smaller than storing both files whole would imply.
        assert len(pool(chunk_store)) < len(a_chunks) + len(b_chunks)

    def test_identical_files_in_distinct_nodes_store_chunks_once(self, chunk_store):
        payload = os.urandom(200_000)
        with chunk_store.create_node(step_type="ingest") as a:
            (a / "f.bin").write_bytes(payload)
        with chunk_store.create_node(step_type="report") as b:
            (b / "f.bin").write_bytes(payload)  # same bytes, different node

        a_chunks = {c for r in manifest(chunk_store, a.node_id).values() for c in r["chunks"]}
        # Every chunk of the second file already existed: the pool didn't grow.
        assert pool(chunk_store) == a_chunks


# ---------------------------------------------------------------------------
# Garbage collection and compaction
# ---------------------------------------------------------------------------


class TestReclaim:
    def test_gc_removes_orphans_keeps_referenced(self, chunk_store):
        with chunk_store.create_node(step_type="ingest") as a:
            (a / "f.bin").write_bytes(os.urandom(120_000))
        with chunk_store.create_node(step_type="ingest") as b:
            (b / "f.bin").write_bytes(os.urandom(120_000))
        b_chunks = {c for r in manifest(chunk_store, b.node_id).values() for c in r["chunks"]}

        age_chunks(chunk_store)
        chunk_store.prune(a, dry_run=False)
        assert pool(chunk_store) == b_chunks  # only a's orphans were removed

    def test_gc_grace_spares_fresh_chunks(self, chunk_store):
        with chunk_store.create_node(step_type="ingest") as a:
            (a / "f.bin").write_bytes(os.urandom(120_000))
        before = len(pool(chunk_store))
        # prune triggers gc immediately; the just-written chunks are too fresh.
        chunk_store.prune(a, dry_run=False)
        assert len(pool(chunk_store)) == before
        # once they age out, a follow-up gc reclaims them.
        age_chunks(chunk_store)
        assert chunk_store.gc() == before
        assert pool(chunk_store) == set()

    def test_compact_repacks_rehydrated_file(self, chunk_store):
        payload = os.urandom(80_000)
        with chunk_store.create_node(step_type="ingest") as node:
            (node / "f.bin").write_bytes(payload)

        # Reading rehydrates a real copy to disk.
        assert (chunk_store.get_node(node.node_id) / "f.bin").exists()
        chunk_store.compact()
        assert not (chunk_store.root / node.node_id / "f.bin").exists()
        # Still transparently readable afterwards.
        assert (chunk_store.get_node(node.node_id) / "f.bin").read_bytes() == payload

    def test_compact_packs_store_created_without_chunking(self, tmp_path):
        plain = ancestree.LineageStore(tmp_path / "s", chunk=False)
        with plain.create_node(step_type="ingest") as node:
            (node / "f.bin").write_bytes(os.urandom(80_000))
        assert (plain.root / node.node_id / "f.bin").exists()  # stored whole

        chunked = ancestree.LineageStore(tmp_path / "s", chunk=True)
        chunked.compact()
        assert not (chunked.root / node.node_id / "f.bin").exists()  # now packed
        assert (chunked.get_node(node.node_id) / "f.bin").exists()  # still readable


# ---------------------------------------------------------------------------
# Interaction with the rest of the lifecycle
# ---------------------------------------------------------------------------


class TestInteraction:
    def test_chunking_off_leaves_files_whole(self, tmp_path):
        store = ancestree.LineageStore(tmp_path / "s", chunk=False)
        with store.create_node(step_type="ingest") as node:
            (node / "f.bin").write_bytes(b"x" * 40_000)
        assert (store.root / node.node_id / "f.bin").exists()
        assert not (store.root / ".chunks").exists()

    def test_failed_run_is_not_packed(self, chunk_store):
        with pytest.raises(RuntimeError):
            with chunk_store.create_node(step_type="ingest") as node:
                (node / "f.bin").write_bytes(b"partial" * 5000)
                raise RuntimeError("boom")
        # Partial work persists as a real file, untouched by chunking.
        assert (chunk_store.root / node.node_id / "f.bin").exists()
        assert not (chunk_store.root / node.node_id / ".artifacts.json").exists()

    def test_dedupe_and_chunk_together(self, tmp_path):
        store = ancestree.LineageStore(tmp_path / "s", dedupe=True, chunk=True)
        payload = os.urandom(60_000)

        def run():
            with store.create_node(step_type="ingest") as node:
                (node / "f.bin").write_bytes(payload)
                node.add_meta("k", 1)
            return node

        first, second = run(), run()
        assert second.node_id == first.node_id  # whole-node dedup still fires
        assert (store.get_node(first.node_id) / "f.bin").read_bytes() == payload


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


class TestIntegrity:
    def test_materialize_detects_corrupted_chunk(self, chunk_store):
        import zlib

        with chunk_store.create_node(step_type="ingest") as node:
            (node / "f.bin").write_bytes(os.urandom(40_000))

        # Overwrite a chunk with valid-but-wrong bytes (decompresses, wrong hash).
        cs = ChunkStore(chunk_store.root)
        victim = next(cs.all_digests())
        cs._path(victim).write_bytes(zlib.compress(b"corrupted"))

        with pytest.raises(RuntimeError, match="integrity"):
            _ = chunk_store.get_node(node.node_id) / "f.bin"
