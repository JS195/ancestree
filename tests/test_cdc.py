"""Phase 3: FastCDC chunker — determinism, bounds, shift-resistance and the
large-file fallback (issue #14). Ports the exact-dedup parts of
test_chunking.py to the new module, plus the round-trip property tests the
exit criteria require."""

import random

from ancestree.ingest.cdc import (
    LARGE_FILE_THRESHOLD,
    MAX_SIZE,
    MIN_SIZE,
    chunk_bytes,
)


def _random_bytes(seed: int, n: int) -> bytes:
    return random.Random(seed).randbytes(n)


def test_roundtrip_and_size_bounds() -> None:
    data = _random_bytes(1, 2_000_000)
    chunks = list(chunk_bytes(data))
    assert b"".join(chunks) == data
    assert len(chunks) > 1
    assert all(MIN_SIZE <= len(c) <= MAX_SIZE for c in chunks[:-1])
    assert 0 < len(chunks[-1]) <= MAX_SIZE


def test_boundaries_are_deterministic() -> None:
    data = _random_bytes(2, 1_000_000)
    assert list(chunk_bytes(data)) == list(chunk_bytes(data))


def test_empty_and_tiny_inputs() -> None:
    assert list(chunk_bytes(b"")) == []
    assert list(chunk_bytes(b"abc")) == [b"abc"]
    small = _random_bytes(3, MIN_SIZE)  # exactly MIN: a single chunk
    assert list(chunk_bytes(small)) == [small]


def test_insertion_shifts_only_local_chunks() -> None:
    # The whole point of content-defined boundaries: an insertion in the
    # middle must leave the vast majority of chunks byte-identical.
    data = _random_bytes(4, 2_000_000)
    mutated = data[:1_000_000] + b"XINSERTEDX" + data[1_000_000:]
    original = set(chunk_bytes(data))
    shifted = set(chunk_bytes(mutated))
    shared = len(original & shifted) / len(original)
    assert shared > 0.5


def test_large_file_fallback_uses_fixed_boundaries() -> None:
    data = _random_bytes(5, MAX_SIZE * 3 + 123)
    chunks = list(chunk_bytes(data, large_file_threshold=1))
    assert [len(c) for c in chunks] == [MAX_SIZE, MAX_SIZE, MAX_SIZE, 123]
    assert b"".join(chunks) == data
    # Below the threshold the gear path is used and cuts differently.
    assert list(chunk_bytes(data)) != chunks
    assert LARGE_FILE_THRESHOLD > MAX_SIZE  # sanity on the default policy


def test_random_mutations_always_roundtrip() -> None:
    # Property test: any sequence of random edits still reassembles exactly,
    # through both the content-defined and fixed-boundary paths.
    rng = random.Random(99)
    data = rng.randbytes(rng.randint(0, 300_000))
    for _ in range(12):
        kind = rng.choice(("insert", "delete", "replace"))
        pos = rng.randint(0, len(data)) if data else 0
        blob = rng.randbytes(rng.randint(1, 10_000))
        if kind == "insert" or not data:
            data = data[:pos] + blob + data[pos:]
        elif kind == "delete":
            data = data[:pos] + data[pos + rng.randint(1, 10_000) :]
        else:
            data = data[:pos] + blob + data[pos + len(blob) :]

        assert b"".join(chunk_bytes(data)) == data
        assert b"".join(chunk_bytes(data, large_file_threshold=1)) == data
