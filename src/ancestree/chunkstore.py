# Python packages
import hashlib
import random
import uuid
import zlib
from pathlib import Path
from typing import Iterator, Union

# ---------------------------------------------------------------------------
# Content-defined chunking (FastCDC)
#
# Splits a file into variable-length chunks at boundaries chosen by the content
# itself, via a Gear rolling hash. Inserting or deleting bytes only shifts the
# boundary of the chunk where the edit lands; every other chunk keeps its bytes
# and therefore its hash, so near-identical files share almost all their chunks.
#
# The Gear table and the chunking parameters are fixed constants: chunk
# boundaries must be reproducible across processes and runs or two identical
# files would chunk differently and fail to deduplicate.
# ---------------------------------------------------------------------------

# A deterministic 256-entry table of 64-bit values, seeded once so every
# process derives the same boundaries.
_rng = random.Random(0xA5A5_5A5A_C3C3_3C3C)
_GEAR = [_rng.getrandbits(64) for _ in range(256)]

_MIN_SIZE = 2 * 1024
_AVG_SIZE = 8 * 1024
_MAX_SIZE = 64 * 1024
_BITS = (_AVG_SIZE).bit_length() - 1  # log2(avg) == 13
# Normalised chunking: a denser mask before the average size makes an early cut
# unlikely; a sparser one after it makes a late cut likely. Chunk sizes cluster
# around the average, away from the min/max extremes.
_MASK_S = (1 << (_BITS + 2)) - 1
_MASK_L = (1 << (_BITS - 2)) - 1
_INT64 = (1 << 64) - 1


def _next_cut(data: bytes, start: int, n: int) -> int:
    """Returns the index one past the end of the chunk beginning at `start`."""
    if n - start <= _MIN_SIZE:
        return n
    normal = min(start + _AVG_SIZE, n)
    hard = min(start + _MAX_SIZE, n)
    fp = 0
    i = start + _MIN_SIZE  # the first _MIN_SIZE bytes can never end a chunk
    while i < normal:
        fp = ((fp << 1) + _GEAR[data[i]]) & _INT64
        if (fp & _MASK_S) == 0:
            return i + 1
        i += 1
    while i < hard:
        fp = ((fp << 1) + _GEAR[data[i]]) & _INT64
        if (fp & _MASK_L) == 0:
            return i + 1
        i += 1
    return hard


def chunk_bytes(data: bytes) -> Iterator[bytes]:
    """Yields the content-defined chunks of `data` in order."""
    start, n = 0, len(data)
    while start < n:
        end = _next_cut(data, start, n)
        yield data[start:end]
        start = end


# ---------------------------------------------------------------------------
# Content-addressed chunk store
#
# A shared pool of chunks under <root>/.chunks, each named by the SHA-256 of its
# bytes and sharded by the first two hex characters. Chunks are immutable and
# self-verifying: writing one that already exists is a no-op, which is exactly
# where deduplication happens.
# ---------------------------------------------------------------------------


class ChunkStore:
    def __init__(self, root: Union[str, Path]) -> None:
        self.dir = Path(root) / ".chunks"

    def _path(self, digest: str) -> Path:
        return self.dir / digest[:2] / digest

    def put(self, data: bytes) -> str:
        """Stores a chunk (compressed) and returns its SHA-256 digest. A chunk
        already present is left untouched — that is the deduplication."""
        digest = hashlib.sha256(data).hexdigest()
        dest = self._path(digest)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Unique temp name per writer so concurrent puts of the same chunk
            # cannot replace each other's temp file mid-write.
            tmp = dest.parent / f"{digest}.{uuid.uuid4().hex}.tmp"
            try:
                tmp.write_bytes(zlib.compress(data))
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
        return digest

    def get(self, digest: str) -> bytes:
        return zlib.decompress(self._path(digest).read_bytes())

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()

    def all_digests(self) -> Iterator[str]:
        if not self.dir.exists():
            return
        for shard in self.dir.iterdir():
            if shard.is_dir():
                for chunk in shard.iterdir():
                    if chunk.is_file() and not chunk.name.endswith(".tmp"):
                        yield chunk.name

    def mtime(self, digest: str) -> float:
        try:
            return self._path(digest).stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def delete(self, digest: str) -> None:
        self._path(digest).unlink(missing_ok=True)
