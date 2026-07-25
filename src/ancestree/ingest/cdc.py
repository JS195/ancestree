"""Content-defined chunking and advanced dedup, in one sectioned module.

Section 1 (this phase) — the FastCDC chunker, Layer 1 of deduplication.
Section 2 — resemblance super-features (Layer 2 discovery, Phase 6).
Section 3 — the zlib-zdict delta codec (Layer 2 storage, Phase 6).

See REBUILD_BLUEPRINT.md section 5.3 (Phase 3, issue #14; Phase 6,
issue #17).
"""

from __future__ import annotations

import random
import sys
import zlib
from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Section 1 — FastCDC chunker (Layer 1)
#
# Splits data into variable-length chunks at boundaries chosen by the content
# itself, via a Gear rolling hash. Inserting or deleting bytes only shifts the
# boundary of the chunk where the edit lands; every other chunk keeps its
# bytes and therefore its digest, so near-identical files share almost all
# their chunks.
#
# The Gear table and the chunking parameters are fixed constants: boundaries
# must be reproducible across processes and runs, or two identical files
# would chunk differently and fail to deduplicate. The constants are
# retunable before a store format bump — a larger MIN_SIZE raises throughput
# (more bytes skipped by the per-byte loop) at a small dedup cost.
# ---------------------------------------------------------------------------

# A deterministic 256-entry table of 64-bit values, seeded once so every
# process derives the same boundaries.
_rng = random.Random(0xA5A5_5A5A_C3C3_3C3C)
_GEAR = [_rng.getrandbits(64) for _ in range(256)]

# AVG_SIZE is deliberately under _ZDICT_MAX (32,256 bytes): zlib's dictionary
# window is 32 KiB, so a chunk larger than that cannot see the whole of its
# delta base and the tail degrades to literals. Sitting at 32 KiB — as v1 did
# — put every average-sized chunk right on that cliff. Halving it is the
# single largest storage win available, and it costs nothing: the per-byte
# Gear loop skips MIN_SIZE bytes per chunk, so smaller chunks mean *more*
# bytes skipped and a faster ingest.
MIN_SIZE = 8 * 1024
AVG_SIZE = 16 * 1024
MAX_SIZE = 256 * 1024
_BITS = (AVG_SIZE).bit_length() - 1  # log2(avg) == 14
# Normalised chunking: a denser mask before the average size makes an early
# cut unlikely; a sparser one after it makes a late cut likely. Chunk sizes
# cluster around the average, away from the min/max extremes.
_MASK_S = (1 << (_BITS + 2)) - 1
_MASK_L = (1 << (_BITS - 2)) - 1
_INT64 = (1 << 64) - 1

#: Artifacts at or above this size skip the pure-Python Gear loop and chunk
#: at fixed MAX_SIZE boundaries (AD4). Whole-file and repeated-content dedup
#: survive (chunks are still content-addressed); only insert-shift resilience
#: is sacrificed — the trade that matters least for huge binaries, in
#: exchange for ingest at C speed (SHA-256 + zlib only).
LARGE_FILE_THRESHOLD = 64 * 1024 * 1024


#: The Gear table reduced to the only bits the boundary tests ever look at.
#: _MASK_S is the wider of the two masks, and carries in `(fp << 1) + gear`
#: propagate upwards only — so the low _MASK_S bits of the fingerprint evolve
#: independently of everything above them. Discarding those high bits leaves
#: every boundary bit for bit identical while keeping the arithmetic on
#: single-digit CPython ints instead of 64-bit ones.
_GEAR_NARROW = [value & _MASK_S for value in _GEAR]


def _next_cut(data: bytes, start: int, n: int) -> int:
    """Returns the index one past the end of the chunk beginning at `start`.

    This is the hot loop of the whole ingest path — one iteration per byte
    of every artifact — so it is written for CPython rather than for looks:
    the tables and masks are bound to locals, and the bytes are walked by
    iterating a slice (a C-speed memcpy, then C-speed iteration) instead of
    indexing, which costs a bounds check and an index increment per byte.
    """
    if n - start <= MIN_SIZE:
        return n
    normal = min(start + AVG_SIZE, n)
    hard = min(start + MAX_SIZE, n)
    gear = _GEAR_NARROW
    mask_s = _MASK_S
    mask_l = _MASK_L
    fingerprint = 0
    # The first MIN_SIZE bytes can never end a chunk.
    base = start + MIN_SIZE
    # Narrowed to mask_s, the "fingerprint & _MASK_S == 0" test is just
    # "fingerprint is zero".
    for offset, byte in enumerate(data[base:normal]):
        fingerprint = ((fingerprint << 1) + gear[byte]) & mask_s
        if not fingerprint:
            return base + offset + 1
    for offset, byte in enumerate(data[normal:hard]):
        fingerprint = ((fingerprint << 1) + gear[byte]) & mask_s
        if not fingerprint & mask_l:
            return normal + offset + 1
    return hard


def _fixed_cuts(data: bytes) -> Iterator[bytes]:
    """MAX_SIZE-aligned slices — the large-file fallback's boundaries."""
    for start in range(0, len(data), MAX_SIZE):
        yield data[start : start + MAX_SIZE]


def chunk_bytes(
    data: bytes, large_file_threshold: int = LARGE_FILE_THRESHOLD
) -> Iterator[bytes]:
    """Yields the content-defined chunks of `data`, in order.

    Below `large_file_threshold` boundaries are content-defined (FastCDC);
    at or above it they are fixed MAX_SIZE slices, keeping huge ingests at
    C speed. Both modes are deterministic: the same bytes always produce
    the same chunks, which is what makes the content-addressed pool
    deduplicate.

    Args:
        data: The artifact bytes to split.
        large_file_threshold: Size at which the fixed-boundary fallback
            engages. The module default is the store's policy; tests pass
            a small value to exercise the fallback cheaply.
    """
    if len(data) >= large_file_threshold:
        yield from _fixed_cuts(data)
        return
    start, n = 0, len(data)
    while start < n:
        end = _next_cut(data, start, n)
        yield data[start:end]
        start = end


# ---------------------------------------------------------------------------
# Section 2 — resemblance (Layer 2 discovery)
#
# Cheap similarity features: min-wise transforms over strided 8-byte samples
# of the chunk. Two chunks sharing ANY feature are delta candidates. An
# in-place edit changes few samples, so the per-transform minimum usually
# survives and near-duplicates keep matching; heavy rewrites change the
# minima and correctly stop matching. Sampling (rather than a per-byte
# rolling min-hash) keeps this at Python-loop-over-hundreds speed instead of
# doubling the per-byte chunking cost — and a wrong candidate costs only a
# wasted trial encode, because ChunkStore keeps a delta only when it
# actually beats plain compression.
# ---------------------------------------------------------------------------

FEATURE_COUNT = 4
_SAMPLE_STRIDE = 64
_SAMPLE_WIDTH = 8
# Fixed odd 64-bit multipliers: each defines an independent min-wise
# transform over the same samples. Constants must never change, or stored
# features would stop matching new ones.
_FEATURE_MULTIPLIERS = (
    0x9E3779B97F4A7C15,
    0xBF58476D1CE4E5B9,
    0x94D049BB133111EB,
    0xD6E8FEB86659FD93,
)
_SIGNED_63 = (1 << 63) - 1  # features stay positive in SQLite's INTEGER
_LITTLE_ENDIAN = sys.byteorder == "little"


def super_features(data: bytes, count: int = FEATURE_COUNT) -> list[int]:
    """The chunk's similarity features (deterministic; empty for tiny
    chunks). Stored in the chunk_feature table for raw chunks so later
    similar chunks can find them as delta bases."""
    if len(data) < _SAMPLE_WIDTH:
        return []
    samples = _samples(data)
    return [
        min([(sample * multiplier) & _INT64 for sample in samples]) & _SIGNED_63
        for multiplier in _FEATURE_MULTIPLIERS[:count]
    ]


def _samples(data: bytes) -> list[int]:
    """The strided 8-byte samples, little-endian, as integers.

    On a little-endian machine the whole extraction is one zero-copy
    ``memoryview`` cast plus a strided ``tolist()`` — both C-speed — instead
    of a Python loop of slices and ``int.from_bytes``. The explicit loop
    stays as the big-endian fallback: features are persisted, so they must
    come out identical on every platform, not merely on this one.
    """
    if _LITTLE_ENDIAN:
        whole_words = memoryview(data)[: (len(data) // _SAMPLE_WIDTH) * _SAMPLE_WIDTH]
        words = whole_words.cast("Q")
        return words[:: _SAMPLE_STRIDE // _SAMPLE_WIDTH].tolist()
    return [
        int.from_bytes(data[i : i + _SAMPLE_WIDTH], "little")
        for i in range(0, len(data) - _SAMPLE_WIDTH + 1, _SAMPLE_STRIDE)
    ]


# ---------------------------------------------------------------------------
# Section 3 — delta codec (Layer 2 storage)
#
# A "delta" is zlib dictionary compression with the base chunk as the preset
# dictionary: DEFLATE back-references into the base ARE the delta, at C
# speed, in ~10 lines, stdlib-only (AD5). DEFLATE's 32 KB window means a
# base contributes at most its trailing 32 KB of reference data — full
# coverage at the 32 KB average chunk size. Depth is capped at 1 by the
# store: a base is always a raw chunk, so decoding costs at most two
# fetches.
# ---------------------------------------------------------------------------


# zlib only finds matches at distances <= w_size - MIN_LOOKAHEAD (32506),
# NOT the full 32768-byte window. A full-32K dictionary therefore puts the
# aligned counterpart of every target byte at distance exactly 32768 — one
# step out of reach — and the "delta" silently degenerates to plain
# compression. Truncating the dictionary's TAIL pulls aligned content back
# inside the reachable window (the last few hundred target bytes just lose
# their reference and emit as literals).
_ZDICT_MAX = 32 * 1024 - 512


def delta_encode(base: bytes, target: bytes) -> bytes:
    """Encodes `target` against `base`. Decodable only with the same base."""
    compressor = zlib.compressobj(zdict=base[:_ZDICT_MAX])
    return compressor.compress(target) + compressor.flush()


def delta_decode(base: bytes, blob: bytes) -> bytes:
    """Reverses delta_encode. Raises zlib.error for a mismatched base."""
    decompressor = zlib.decompressobj(zdict=base[:_ZDICT_MAX])
    return decompressor.decompress(blob) + decompressor.flush()
