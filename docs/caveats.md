# Caveats & Limitations

Ancestree is deliberately small: the whole store is one SQLite database, and every write is a transaction. That design buys simplicity and crash-safety, but it also has sharp edges worth knowing before you lean on it. This page collects the behaviours that surprise people.

## Rules & policy are set once

Rules, generation triggers and the `dedup`/`chunk` policy are persisted inside the store's database the first time it is created, and read back on every subsequent open. **They cannot be changed afterwards.** Passing different values to an existing store gives a warning and the stored configuration wins. To change them, start a new store.

Rules are also only as strict as you make them. A `step_type` that does not appear in `rules` has *no* transition constraint and can be created under any parent; rules only restrict the step types you actually list. A store created with no rules permits every transition.

## Nodes can vanish by design

`create_node` only persists a node if you write at least one artifact or add your own metadata inside the block. An untouched node is discarded when the context manager exits, with a `UserWarning` rather than an exception.

If your code raises inside the `create_node` block, any partial work is kept and the node is flagged `healthy=False` before the exception re-raises. A node existing in the store therefore does not mean its block ran to completion — check the `healthy` flag, which is searchable via `find(healthy=False)`. If a run is killed outright (SIGKILL, power loss), whatever it managed to write is adopted as an unhealthy node the next time the store opens.

## Don't hold artifact paths across a block boundary

Inside a `create_node` block, `node / "file"` points at a transient scratch directory that is deleted once the node commits. Reads after that hand back a path in the session cache (`<root>/.cache/`), regenerated from the store on demand. Both are real, readable paths — but re-fetch them through `node / "file"` or `artifacts()` each time rather than stashing one and using it later.

## The web graph

`generate_web_graph()` writes a single self-contained `interactive_pipeline.html` at the store root, **overwriting any existing one**. It is a **view-only snapshot** — the searchable explorer with node diffs and the runs table is the live server (`store.host_live_graph()` or `python -m ancestree serve`). Because everything is inlined into one file (small images as data URIs; larger artifacts copied beside it), very large stores produce very large HTML.

## Write and read costs

Your code writes files at native speed inside the block; the price is paid at block exit, where the artifact is chunked, hashed, compressed and committed. The chunking loop is pure Python (a few MB/s), so the cost scales with megabytes — files of 64 MiB and above switch to fixed boundaries at C speed. The first read of an artifact in a session reassembles it from chunks; every read after that is a plain file read from the cache. The [CDC deep dive notebook](examples/cdc_deep_dive.ipynb) measures all of this against a native baseline.

## Concurrency

SQLite allows many readers but one writer at a time. Concurrent processes can share a store — a writer simply waits its turn (up to the busy timeout) — but heavy parallel writing is not what this is for. A single `LineageStore` instance serialises its own writes internally.

Opening a store runs a sweep for scratch directories orphaned by a crashed session. That sweep never touches a node another process is still writing: a node is assembled in a staging directory and renamed into place only once it carries its crash-recovery seed, so an in-flight node is never mistaken for litter.

## NFS

**Keep stores on local disk.** SQLite file locking over NFS is unreliable, and the 0.1.x file-based NFS guarantee did not survive the move to a database backend.

## One file is the whole store — but a *live* store is three

`ancestree.db` is the single source of truth — there is no side index to rebuild and no directory tree to fall back on. That cuts both ways: a corrupt database is real data loss. WAL journalling protects against crashes mid-write, `PRAGMA integrity_check` (reachable via `store.sql`) verifies the file, and `store.export()` writes grep-able per-node `meta.json` sidecars whenever you want plain files on record.

**Backing up needs one moment's care.** While a store is open, WAL journalling keeps recent commits in `ancestree.db-wal`, not in `ancestree.db`. Copying `ancestree.db` alone out from under an open store therefore gives you a perfectly valid, perfectly **empty** store — silently, with no error. Any one of these is correct:

- `store.backup(dest)` — a consistent single-file copy via SQLite's online backup API, safe to take at any time, including while another thread is writing. `dest` ending in `.db` writes that file; anything else is treated as a store root you can open directly.
- copy `ancestree.db`, `ancestree.db-wal` and `ancestree.db-shm` together;
- `store.close()` first (which checkpoints), then copy the single file.

`store.stats()["database_bytes"]` counts the database and its WAL together, so it reflects what the store actually occupies rather than what has been checkpointed so far.

## Scale

Searches are answered by indexed SQL rather than linear scans, and opening a store no longer replays an index — cold opens are effectively instant. The practical limits are the write path (chunking cost scales with artifact megabytes) and the snapshot (one HTML file inlining everything). The [branching stress test](examples/stress_branching.ipynb) exercises a few hundred nodes; the 10k-node territory the old benchmark measured is comfortably in range for queries.

## Metadata coercion and overwrites

The structural keys the store owns — `node_id`, `parent_id`, `step_type`, `generation`, `healthy`, `created_utc`, `created_epoch`, `duration_s`, `size_bytes`, `content_hash` — are reserved. `add_meta` raises `ValueError` if you try to set one, so you cannot accidentally shadow the facts the store depends on for lineage, recency and health. They are attributes on the returned records, not metadata entries.

`table` and `json` entries are always stored non-searchable, and `image`/`link` entries that point at files (rather than URLs) are rewritten relative to the node and also forced non-searchable. They render in the explorers but cannot be matched by `find`.

The default `auto` data type infers the rendering from the value's *type*, not by sniffing string contents: a `Path` becomes an image (by file suffix) or a file link, a `dict`/`list` becomes JSON, a DataFrame becomes a table, and an `http(s)://` string becomes a link. Any other string stays plain `text`. numpy/pandas scalars, arrays, sets and datetimes are coerced to native Python with a warning; anything still unserialisable is rejected at the `add_meta` call.

## Deduplication surprises

With `dedup` on (the default), re-running a step whose content — step type, parents, user metadata and artifact bytes — is identical gives you the *same node back*: the `with` block's variable is rebound onto the existing node and nothing new is stored. Change any of those and you get a distinct node. Failed (unhealthy) runs never merge. If you want every run recorded regardless, create the store with `dedup=False`.

## Paths and artifacts

`artifacts(contains=...)` matches both as a glob and as a case-insensitive substring anywhere in the filename.

## Automatic provenance

Every node silently records who and what produced it: the OS user, Python version, platform, and the current git commit, branch, and dirty state. The git fields are captured by shelling out to `git`, which means a few subprocesses per node, and means your identity and repository state are recorded by default. Provenance is display-only (not searchable). Outside a git repository, or without git installed, the git fields are simply `None`.

## Search semantics

A predicate passed to `find` receives `None` for any key the node lacks, so a blanket-true predicate such as `lambda v: True` matches *every* node, including ones missing that key. A predicate that raises is treated as "no match" but provides a warning to the user that an error was raised.

`latest` ranks by the stored creation time. Nodes created in the same instant tie, and which one is returned is arbitrary — highly unlikely, but worth noting as an extremely rare edge case.
