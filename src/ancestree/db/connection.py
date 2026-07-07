"""ConnectionManager — the one owner of the SQLite connection rules.

WAL, synchronous=NORMAL, foreign_keys, busy_timeout, mmap; thread-local read
connections; a serialized write() transaction; PID rebind after fork; WAL
checkpoint after large ingests.

See REBUILD_BLUEPRINT.md section 5.3. Arrives in Phase 1 (issue #12).
"""
