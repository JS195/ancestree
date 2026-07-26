"""Phase 6: Layer-2 dedup (issue #17) — resemblance super-features, the
zlib-zdict delta codec, depth-1 enforcement, compact's live-base closure,
and end-to-end round-trips through a delta-enabled store."""

from __future__ import annotations

import random
import zlib
from pathlib import Path

import pytest

from ancestree.db.chunk_store import ChunkStore
from ancestree.db.connection import ConnectionManager
from ancestree.db.metadata_store import MetadataStore, NodeRecord
from ancestree.ingest.cdc import delta_decode, delta_encode, super_features
from ancestree.maintenance import compact_chunks
from ancestree.store import LineageStore


def _mutate(data: bytes, edits: int, seed: int) -> bytes:
    """Scattered in-place byte edits: the near-duplicate class Layer 2
    exists for (re-encoded values, tweaked configs), where Layer 1's
    boundaries survive but almost every chunk differs slightly."""
    rng = random.Random(seed)
    out = bytearray(data)
    for _ in range(edits):
        out[rng.randrange(len(out))] = rng.randrange(256)
    return bytes(out)


# ---------------------------------------------------------------------------
# Resemblance features
# ---------------------------------------------------------------------------


def test_super_features_similarity_semantics() -> None:
    data = random.Random(1).randbytes(32_768)
    assert super_features(data) == super_features(data)  # deterministic

    similar = _mutate(data, 50, seed=2)
    assert set(super_features(data)) & set(super_features(similar))

    unrelated = random.Random(3).randbytes(32_768)
    assert not set(super_features(data)) & set(super_features(unrelated))

    assert super_features(b"tiny") == []  # below one sample: no features


# ---------------------------------------------------------------------------
# Delta codec
# ---------------------------------------------------------------------------


def test_delta_codec_roundtrips_and_wins_on_near_duplicates() -> None:
    base = random.Random(4).randbytes(32_768)
    target = _mutate(base, 60, seed=5)

    blob = delta_encode(base, target)
    assert delta_decode(base, blob) == target
    # Random bytes barely compress alone; against the base they collapse.
    assert len(blob) < len(zlib.compress(target)) * 0.2

    with pytest.raises(zlib.error):
        delta_decode(random.Random(6).randbytes(32_768), blob)


# ---------------------------------------------------------------------------
# ChunkStore Layer-2 behaviour
# ---------------------------------------------------------------------------

Env = tuple[ConnectionManager, ChunkStore]


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    manager = ConnectionManager(tmp_path / "s.db")
    return manager, ChunkStore(manager, delta=True)


def _kinds(manager: ConnectionManager) -> dict[str, tuple[int, str | None]]:
    rows = (
        manager.read().execute("SELECT digest, kind, base_digest FROM chunk").fetchall()
    )
    return {row["digest"]: (row["kind"], row["base_digest"]) for row in rows}


def test_similar_chunk_is_stored_as_delta(env: Env) -> None:
    manager, chunk_store = env
    base_data = random.Random(7).randbytes(32_768)
    similar = _mutate(base_data, 40, seed=8)

    with manager.write() as conn:
        base_digest = chunk_store.put_chunk(conn, base_data)
        similar_digest = chunk_store.put_chunk(conn, similar)

    kinds = _kinds(manager)
    # randbytes is incompressible, so the base lands as kind 2 (verbatim);
    # what matters is that it is a base, not a delta.
    assert kinds[base_digest] == (2, None)
    assert kinds[similar_digest] == (1, base_digest)
    assert chunk_store.get_chunk(similar_digest) == similar  # verified


def test_dissimilar_chunk_stays_raw(env: Env) -> None:
    manager, chunk_store = env
    with manager.write() as conn:
        chunk_store.put_chunk(conn, random.Random(9).randbytes(32_768))
        other = chunk_store.put_chunk(conn, random.Random(10).randbytes(32_768))
    assert _kinds(manager)[other] == (2, None)  # a base, not a delta


def test_delta_depth_is_capped_at_one(env: Env) -> None:
    manager, chunk_store = env
    v0 = random.Random(11).randbytes(32_768)
    v1 = _mutate(v0, 30, seed=12)
    v2 = _mutate(v1, 30, seed=13)

    with manager.write() as conn:
        d0 = chunk_store.put_chunk(conn, v0)
        d1 = chunk_store.put_chunk(conn, v1)
        d2 = chunk_store.put_chunk(conn, v2)

    kinds = _kinds(manager)
    assert kinds[d1] == (1, d0)
    # v2 resembles v1 most, but only RAW chunks qualify as bases: its base
    # must be the raw v0 — never a chain through the delta v1.
    assert kinds[d2] == (1, d0)
    assert chunk_store.get_chunk(d2) == v2


def test_delta_policy_off_stores_everything_raw(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "s.db")
    chunk_store = ChunkStore(manager, delta=False)
    base_data = random.Random(14).randbytes(32_768)
    with manager.write() as conn:
        chunk_store.put_chunk(conn, base_data)
        chunk_store.put_chunk(conn, _mutate(base_data, 40, seed=15))
    assert all(kind != 1 for kind, _ in _kinds(manager).values())
    count = manager.read().execute("SELECT count(*) AS n FROM chunk_feature").fetchone()
    assert count["n"] == 0  # no resemblance index without the policy


def test_compact_keeps_live_delta_bases(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "s.db")
    metadata = MetadataStore(manager)
    chunk_store = ChunkStore(manager, delta=True)
    metadata.add_node(
        NodeRecord(
            node_id="n1",
            step_type="step",
            generation=0,
            created_utc="t",
            created_epoch=0.0,
            healthy=True,
        )
    )
    base_data = random.Random(16).randbytes(32_768)
    similar = _mutate(base_data, 40, seed=17)
    import hashlib

    with manager.write() as conn:
        chunk_store.put_chunk(conn, base_data)  # raw; referenced by nothing
        delta_digest = chunk_store.put_chunk(conn, similar)  # delta on base
        chunk_store.add_artifact(
            conn,
            "n1",
            "only.bin",
            len(similar),
            hashlib.sha256(similar).hexdigest(),
            [delta_digest],
        )

    # The base has no artifact reference, but it is the base of a live
    # delta: the one-hop closure must keep BOTH.
    assert compact_chunks(manager) == 0
    assert chunk_store.artifact_bytes("n1", "only.bin") == similar

    metadata.remove("n1")
    assert compact_chunks(manager) == 2  # delta and its base go together


# ---------------------------------------------------------------------------
# End to end through the store
# ---------------------------------------------------------------------------


def test_near_duplicate_versions_shrink_the_store(tmp_path: Path) -> None:
    payload = random.Random(18).randbytes(200_000)
    versions = [
        _mutate(payload, 2_000, seed=19 + i) for i in range(3)
    ]  # ~1% scattered edits: every chunk differs, Layer 1 shares nothing

    def fill(root: Path, delta_policy: bool) -> int:
        store = LineageStore(root, delta=delta_policy)
        for index, version in enumerate(versions):
            with store.create_node(step_type="version") as node:
                (node / "data.bin").write_bytes(version)
                node.add_meta("v", index)
        for record in store.find():
            assert (record / "data.bin").read_bytes() == versions[
                record.metadata["v"]["value"]
            ]
        stored = int(store.stats()["chunk_stored_bytes"])
        store.close()
        return stored

    layer1_only = fill(tmp_path / "l1", delta_policy=False)
    layer2 = fill(tmp_path / "l2", delta_policy=True)
    # With only 3 versions the raw first copy dominates, and randbytes is
    # incompressible so every chunk pays full per-chunk overhead with nothing
    # for zlib to reclaim; the amortized ratio over many versions is measured
    # by benchmarks/layer2.py.
    assert layer2 < layer1_only * 0.75


def test_random_mutations_roundtrip_through_a_delta_store(
    tmp_path: Path,
) -> None:
    store = LineageStore(tmp_path / "proj", delta=True)
    rng = random.Random(99)
    payload = rng.randbytes(120_000)
    for round_number in range(6):
        payload = _mutate(payload, rng.randint(1, 3_000), seed=rng.randint(0, 9999))
        with store.create_node(step_type="evolve") as node:
            (node / "state.bin").write_bytes(payload)
            node.add_meta("round", round_number)
        record = store.get(node)
        assert record is not None
        assert (record / "state.bin").read_bytes() == payload
