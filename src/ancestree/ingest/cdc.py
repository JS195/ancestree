"""Content-defined chunking and advanced dedup, in one sectioned module.

Section 1 (this phase) — the FastCDC chunker, Layer 1 of deduplication.
Section 2 — resemblance super-features (Layer 2 discovery, Phase 6).
Section 3 — the zlib-zdict delta codec (Layer 2 storage, Phase 6).

See REBUILD_BLUEPRINT.md section 5.3 (Phase 3, issue #14; Phase 6,
issue #17).
"""

from __future__ import annotations

import random
from typing import Iterator

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

MIN_SIZE = 8 * 1024
AVG_SIZE = 32 * 1024
MAX_SIZE = 256 * 1024
_BITS = (AVG_SIZE).bit_length() - 1  # log2(avg) == 15
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


def _next_cut(data: bytes, start: int, n: int) -> int:
    """Returns the index one past the end of the chunk beginning at `start`."""
    if n - start <= MIN_SIZE:
        return n
    normal = min(start + AVG_SIZE, n)
    hard = min(start + MAX_SIZE, n)
    fingerprint = 0
    i = start + MIN_SIZE  # the first MIN_SIZE bytes can never end a chunk
    while i < normal:
        fingerprint = ((fingerprint << 1) + _GEAR[data[i]]) & _INT64
        if (fingerprint & _MASK_S) == 0:
            return i + 1
        i += 1
    while i < hard:
        fingerprint = ((fingerprint << 1) + _GEAR[data[i]]) & _INT64
        if (fingerprint & _MASK_L) == 0:
            return i + 1
        i += 1
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
# Section 2 — resemblance (Layer 2 discovery) — arrives in Phase 6 (#17):
# min-wise super-features computed in the chunking pass, backing the
# chunk_feature lookup for similar-but-not-identical base chunks.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Section 3 — delta codec (Layer 2 storage) — arrives in Phase 6 (#17):
# encode/decode via zlib dictionary compression with a raw base chunk as
# zdict (AD5); delta depth is capped at 1.
# ---------------------------------------------------------------------------
