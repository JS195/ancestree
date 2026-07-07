# Python packages
import atexit
import hashlib
import os
import random
import shutil
import uuid
import zlib
from pathlib import Path
from typing import Dict, Iterator, Optional, Union

try:
    import fcntl  # POSIX advisory locks; absent on Windows
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

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

_MIN_SIZE = 8 * 1024
_AVG_SIZE = 32 * 1024
_MAX_SIZE = 256 * 1024
_BITS = (_AVG_SIZE).bit_length() - 1  # log2(avg) == 15
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


# ---------------------------------------------------------------------------
# Session-scoped read cache
#
# Reading a packed artifact reassembles its bytes; rather than writing that copy
# back into the node directory (which would re-bloat the store and force a manual
# compact()), it is written into a content-addressed cache under <root>/.cache.
# The cache is pure derived data — anything in it can be regenerated from the
# chunk pool — so it is disposable:
#
#   * each process gets its own session subdirectory, so concurrent sessions
#     never delete each other's files;
#   * the session is wiped when the process exits (atexit) or the store's
#     context manager closes;
#   * a session that crashed without cleaning up is reaped on the next startup
#     via a per-session lock file the OS releases on process death.
#
# A cached copy is scoped to its node (<session>/<node_id>/<rel>) so its path
# reads like the node it belongs to.
# ---------------------------------------------------------------------------


class ReadCache:
    """A per-process, content-addressed cache of reassembled artifacts living
    under ``<root>/.cache/<session>``. See the module comment above for the
    lifecycle. Construct via :func:`get_read_cache`, which keeps one instance
    per store root."""

    def __init__(self, root: Union[str, Path]) -> None:
        self.base = Path(root) / ".cache"
        # pid is human-meaningful; the uuid suffix defeats pid reuse.
        self.session = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.dir = self.base / self.session
        self._lock_path = self.base / f"{self.session}.lock"
        self._lock_fd: Optional[int] = None
        self.base.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        self._reap_dead_sessions()
        atexit.register(self.cleanup)

    def _acquire_lock(self) -> None:
        """Hold an exclusive lock for this session's lifetime. The OS releases it
        whenever the process ends — cleanly or not — which is what lets a later
        session detect that this one is gone."""
        if fcntl is None:
            return
        try:
            self._lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_fd = None  # locking unavailable; rely on atexit + startup

    def _reap_dead_sessions(self) -> None:
        """Delete cache directories whose owning process is gone. A sibling lock
        we can acquire means its holder has died (the lock is released on process
        exit), so that session's directory is safe to remove."""
        if fcntl is None:
            return
        for lock in self.base.glob("*.lock"):
            if lock.name == self._lock_path.name:
                continue
            try:
                fd = os.open(lock, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue  # still held -> owner alive -> leave it
            try:
                shutil.rmtree(self.base / lock.stem, ignore_errors=True)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                lock.unlink(missing_ok=True)

    def path_for(self, node_id: str, rel: str) -> Path:
        """The cache path a node's artifact reassembles to. Scoped by node id and
        the artifact's own relative path, so the location reads like the node it
        belongs to (``<root>/.cache/<session>/<node_id>/<rel>``) and re-reading
        the same artifact within a session reuses it. Copies are not shared
        across nodes — this is a disposable session cache, not the chunk pool."""
        out = self.dir / node_id / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        return out

    def cleanup(self) -> None:
        """Wipe this session's cache directory and release its lock. Idempotent;
        called on context-manager exit and at interpreter shutdown."""
        shutil.rmtree(self.dir, ignore_errors=True)
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        self._lock_path.unlink(missing_ok=True)


# One cache per store root per process, created on first packed read.
_read_caches: Dict[str, ReadCache] = {}


def get_read_cache(root: Union[str, Path]) -> ReadCache:
    """Returns the process's :class:`ReadCache` for ``root``, creating it on
    first use."""
    key = str(Path(root).resolve())
    cache = _read_caches.get(key)
    if cache is None:
        cache = ReadCache(key)
        _read_caches[key] = cache
    return cache


def drop_read_cache(root: Union[str, Path]) -> None:
    """Wipes and forgets the read cache for ``root`` (used by the store's
    context-manager exit / ``clear_cache``). A later read lazily recreates it."""
    key = str(Path(root).resolve())
    cache = _read_caches.pop(key, None)
    if cache is not None:
        cache.cleanup()
