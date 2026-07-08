"""ConnectionManager — the one owner of the SQLite connection rules.

SQLite connections must not be shared carelessly across threads and must
never be reused across a ``fork()``. Every one of those rules lives here:

* one connection per thread, created lazily and configured identically;
* a connection inherited across a fork is abandoned in the child — never
  reused, never closed (the parent still owns the underlying file state) —
  and transparently replaced on first use, detected by PID;
* writes are serialised through one in-process lock and run inside an
  explicit ``BEGIN IMMEDIATE`` transaction: one writer per process by the
  lock, one writer per database by SQLite's own file lock, waited on via
  ``busy_timeout``;
* ``checkpoint()`` folds the WAL back into the database after a large
  ingest so the sidecar never balloons.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 1, issue #12).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

from .schema import ensure_schema

# Patient enough to sit out another process's large ingest transaction.
_BUSY_TIMEOUT_MS = 30_000
# Reads go through the OS page cache via memory-map; harmless when the
# database is smaller than this.
_MMAP_BYTES = 256 * 1024 * 1024


def _configure(conn: sqlite3.Connection) -> None:
    """Applies the per-connection pragmas. Database-level settings
    (journal_mode=WAL, auto_vacuum) are persistent and set once by
    ensure_schema at creation."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute(f"PRAGMA mmap_size = {_MMAP_BYTES}")


class ConnectionManager:
    """Owns every SQLite connection to one ``ancestree.db``.

    Constructing the manager opens (creating if absent) the database and
    verifies its schema, so a foreign or corrupt file fails fast here rather
    than on the first query.
    """

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._registry_lock = threading.Lock()
        # (owner pid, connection): close() must only close connections this
        # process created — entries inherited across a fork belong to the
        # parent and are abandoned, never closed.
        self._registry: List[Tuple[int, sqlite3.Connection]] = []
        self._closed = False
        try:
            ensure_schema(self.read())
        except BaseException:
            self.close()
            raise

    def read(self) -> sqlite3.Connection:
        """This thread's connection, created on first use.

        Runs in autocommit outside ``write()`` — fine for reads, which under
        WAL never block a writer or each other. A connection created before
        a ``fork`` fails the PID check in the child and is replaced with a
        fresh one; the inherited object is deliberately never touched.
        """
        if self._closed:
            raise RuntimeError("This ConnectionManager has been closed.")
        slot: Optional[Tuple[int, sqlite3.Connection]] = getattr(
            self._local, "slot", None
        )
        pid = os.getpid()
        if slot is not None and slot[0] == pid:
            return slot[1]
        # check_same_thread=False solely so close() can release every
        # connection from one thread; each connection is still used by
        # exactly one thread via the thread-local slot.
        conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        _configure(conn)
        self._local.slot = (pid, conn)
        with self._registry_lock:
            self._registry.append((pid, conn))
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A serialised write transaction on this thread's connection.

        ``BEGIN IMMEDIATE`` takes the database write lock up front, so a
        transaction can never fail on a mid-way lock upgrade. Commits on
        clean exit, rolls back on any exception (which is re-raised).
        Reads inside the block see the transaction's own writes.
        """
        conn = self.read()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def checkpoint(self) -> None:
        """Folds the WAL into the database file and truncates it to zero.

        Called after a large ingest so the sidecar never balloons. Best
        effort: a concurrent reader in another process can defer the
        truncation to a later checkpoint.
        """
        self.read().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        """Closes every connection this process created. Idempotent.

        Connections inherited across a fork are abandoned instead — closing
        them could release file locks the parent still relies on.
        """
        self._closed = True
        pid = os.getpid()
        with self._registry_lock:
            for owner_pid, conn in self._registry:
                if owner_pid == pid:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass  # already closed or mid-use; abandon it
            self._registry.clear()
        self._local = threading.local()
