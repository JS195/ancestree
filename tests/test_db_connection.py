"""Phase 1: ConnectionManager threading, transactions and fork-safety
(issue #12)."""

import os
import threading
from pathlib import Path
from typing import List

import pytest

from ancestree.db.connection import ConnectionManager


def test_same_thread_reuses_one_connection(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    assert mgr.read() is mgr.read()
    mgr.close()


def test_each_thread_gets_its_own_connection(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    main_conn = mgr.read()
    seen: List[object] = []
    thread = threading.Thread(target=lambda: seen.append(mgr.read()))
    thread.start()
    thread.join()
    assert seen[0] is not main_conn
    mgr.close()


def test_write_commits_and_is_visible_to_other_threads(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    with mgr.write() as conn:
        conn.execute("INSERT INTO config VALUES ('k', 'v')")

    seen: List[object] = []

    def reader() -> None:
        row = mgr.read().execute(
            "SELECT value FROM config WHERE key = 'k'"
        ).fetchone()
        seen.append(row["value"] if row is not None else None)

    thread = threading.Thread(target=reader)
    thread.start()
    thread.join()
    assert seen == ["v"]
    mgr.close()


def test_write_rolls_back_on_exception(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    with pytest.raises(RuntimeError, match="boom"):
        with mgr.write() as conn:
            conn.execute("INSERT INTO config VALUES ('k', 'v')")
            raise RuntimeError("boom")
    count = mgr.read().execute("SELECT count(*) AS n FROM config").fetchone()
    assert count["n"] == 0
    mgr.close()


def test_writes_serialise_across_threads(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    with mgr.write() as conn:
        conn.execute("INSERT INTO config VALUES ('n', '0')")

    def bump() -> None:
        for _ in range(25):
            with mgr.write() as conn:
                row = conn.execute(
                    "SELECT value FROM config WHERE key = 'n'"
                ).fetchone()
                conn.execute(
                    "UPDATE config SET value = ? WHERE key = 'n'",
                    (str(int(row["value"]) + 1),),
                )

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    row = mgr.read().execute("SELECT value FROM config WHERE key = 'n'").fetchone()
    assert row["value"] == "100"  # no lost updates
    mgr.close()


def test_readers_are_not_blocked_by_an_open_write(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    with mgr.write() as conn:
        conn.execute("INSERT INTO config VALUES ('k', 'before')")

    in_txn = threading.Barrier(2)
    release = threading.Barrier(2)

    def writer() -> None:
        with mgr.write() as conn:
            conn.execute("UPDATE config SET value = 'after' WHERE key = 'k'")
            in_txn.wait(timeout=10)  # transaction now open, uncommitted
            release.wait(timeout=10)  # hold it open until the read is done

    thread = threading.Thread(target=writer)
    thread.start()
    in_txn.wait(timeout=10)
    try:
        # Under WAL a reader on another connection is neither blocked nor
        # shown the uncommitted write — it reads the last committed snapshot.
        row = mgr.read().execute(
            "SELECT value FROM config WHERE key = 'k'"
        ).fetchone()
        assert row["value"] == "before"
    finally:
        release.wait(timeout=10)
        thread.join(timeout=10)

    row = mgr.read().execute("SELECT value FROM config WHERE key = 'k'").fetchone()
    assert row["value"] == "after"
    mgr.close()


def test_checkpoint_truncates_the_wal(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    mgr = ConnectionManager(db)
    with mgr.write() as conn:
        for i in range(200):
            conn.execute("INSERT INTO config VALUES (?, 'x')", (f"k{i}",))

    wal = tmp_path / "s.db-wal"
    assert wal.exists() and wal.stat().st_size > 0
    mgr.checkpoint()
    assert wal.stat().st_size == 0
    mgr.close()


def test_closed_manager_refuses_use_and_close_is_idempotent(
    tmp_path: Path,
) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    mgr.close()
    mgr.close()  # idempotent
    with pytest.raises(RuntimeError):
        mgr.read()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX-only")
def test_forked_child_gets_a_fresh_connection(tmp_path: Path) -> None:
    mgr = ConnectionManager(tmp_path / "s.db")
    with mgr.write() as conn:
        conn.execute("INSERT INTO config VALUES ('from', 'parent')")
    parent_conn = mgr.read()

    pid = os.fork()
    if pid == 0:
        # In the child: the manager must hand out a NEW connection (the
        # inherited one is abandoned), and reads and writes must both work.
        status = 1
        try:
            child_conn = mgr.read()
            fresh = child_conn is not parent_conn
            row = child_conn.execute(
                "SELECT value FROM config WHERE key = 'from'"
            ).fetchone()
            with mgr.write() as conn:
                conn.execute("INSERT INTO config VALUES ('from-child', 'yes')")
            if fresh and row is not None and row["value"] == "parent":
                status = 0
        finally:
            os._exit(status)

    _, exit_status = os.waitpid(pid, 0)
    assert os.WIFEXITED(exit_status) and os.WEXITSTATUS(exit_status) == 0
    # The parent's connection still works and sees the child's commit.
    row = mgr.read().execute(
        "SELECT value FROM config WHERE key = 'from-child'"
    ).fetchone()
    assert row is not None and row["value"] == "yes"
    mgr.close()
