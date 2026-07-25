# Caveats & Limitations

The whole store is one SQLite database and every write is a transaction. That buys simplicity and crash safety, with consequences worth knowing before you rely on it.

## Rules and policy are set once

Rules, generation triggers and the `dedup`/`chunk` policy are written into the database at creation and read back on every later open. They cannot be changed afterwards. Passing different values to an existing store warns, and the stored configuration wins. To change them, start a new store.

Rules only restrict the step types you list. A `step_type` absent from `rules` has no transition constraint and can be created under any parent, and a store created with no rules permits everything.

## Nodes can vanish by design

`create_node` only persists a node if you write an artifact or add metadata inside the block. An untouched node is discarded on exit with a `UserWarning` rather than an exception.

If your code raises inside the block, partial work is kept and the node is flagged `healthy=False` before the exception re-raises. A node existing in the store therefore does not mean its block completed, so check the `healthy` flag, searchable via `find(healthy=False)`. If a run is killed by SIGKILL or power loss, whatever it wrote is adopted as an unhealthy node at the next store open.

## Don't hold artifact paths across a block boundary

Inside a `create_node` block, `node / "file"` points at a scratch directory deleted once the node commits. Reads after that return a path in the session cache (`<root>/.cache/`), regenerated on demand. Both are real, readable paths, but re-fetch them through `node / "file"` or `artifacts()` each time rather than reusing a stored one.

## `prune` is permanent, and reclaims the space

`prune(node)` defaults to a dry run, returning the nodes that would go (the node plus all descendants) without changing anything. `dry_run=False` deletes them, then compacts: unreferenced chunks are dropped and the database file shrinks in place. There is no undo, and after compaction the bytes are gone rather than merely unreferenced.

Compaction scans the whole chunk pool, so pruning in a loop repeats that work. Pass `compact=False` and call `store.compact()` once at the end for the same result.

## The web graph

`generate_web_graph()` writes a self-contained `interactive_pipeline.html` at the store root, overwriting any existing one. It is view-only; the searchable explorer with diffs and the runs table is the live server (`store.host_live_graph()` or `python -m ancestree serve`). Everything is inlined into one file, with small images as data URIs and larger artifacts copied beside it, so very large stores produce very large HTML.

## Write and read costs

Your code writes files at native speed inside the block. The cost is paid at block exit, where the artifact is chunked, hashed, compressed and committed. The chunking loop is pure Python at roughly 27 MB/s, so it scales with megabytes; files of 64 MiB and above switch to fixed boundaries at C speed. The first read of an artifact in a session reassembles it from chunks, and every read after that is a plain file read from the cache. `benchmarks/RESULTS.md` measures all of this.

## Concurrency

SQLite allows many readers but one writer at a time. Concurrent processes can share a store, with writers waiting their turn up to the busy timeout, but heavy parallel writing is not what this is for. A single `LineageStore` instance serialises its own writes.

Opening a store sweeps for scratch directories orphaned by a crashed session. The sweep never touches a node another process is still writing: a node is assembled in a staging directory and renamed into place only once it carries its crash-recovery seed, so in-flight nodes are not mistaken for litter.

## A store is tied to the version that wrote it

Every store records its format version. Ancestree checks it and refuses anything it did not write; it never converts a store. Migration is not a goal of this project.

A store written by an older or newer version will not open, giving an explanatory error rather than a silent misread. Nothing is modified on a failed open. To read an old store, keep the version that wrote it installed; both remain on PyPI. Treat a store as data you can keep, but not something to carry across upgrades.

## NFS

Keep stores on local disk. SQLite file locking over NFS is unreliable, and the 0.1.x file-based NFS guarantee did not survive the move to a database backend.

## One file is the whole store, but a live store is three

`ancestree.db` is the single source of truth. There is no side index to rebuild and no directory tree to fall back on, which also means a corrupt database is real data loss. WAL journalling protects against crashes mid-write, `PRAGMA integrity_check` (via `store.sql`) verifies the file, and `store.export()` writes `meta.json` sidecars whenever you want plain files on record.

Backing up needs care. While a store is open, WAL journalling keeps recent commits in `ancestree.db-wal` rather than `ancestree.db`. Copying `ancestree.db` alone out from under an open store gives a valid but empty store, silently. Any of these is correct:

- `store.backup(dest)`, a consistent single-file copy via SQLite's online backup API, safe at any time including while another thread writes. A `dest` ending in `.db` writes that file; anything else is treated as a store root.
- Copy `ancestree.db`, `ancestree.db-wal` and `ancestree.db-shm` together.
- Call `store.close()`, which checkpoints, then copy the single file.

`store.stats()["database_bytes"]` counts the database and WAL together, reflecting what the store occupies rather than what has been checkpointed.

## Scale

Searches are answered by indexed SQL rather than linear scans, and opening a store does not replay an index, so cold opens are effectively instant. The practical limits are the write path, where chunking scales with artifact megabytes, and the snapshot, which inlines everything into one HTML file. A few thousand nodes is comfortable: `find()` over 3000 nodes runs in about 26 ms, a selective metadata lookup in about 0.04 ms.

## Metadata coercion and overwrites

The structural keys the store owns (`node_id`, `parent_id`, `step_type`, `generation`, `healthy`, `created_utc`, `created_epoch`, `duration_s`, `size_bytes`, `content_hash`) are reserved. `add_meta` raises `ValueError` on them, so you cannot shadow the facts the store relies on for lineage, recency and health. They are attributes on the returned records, not metadata entries.

`table` and `json` entries are always stored non-searchable. `image` and `link` entries pointing at files rather than URLs are rewritten relative to the node and also forced non-searchable. They render in the explorers but cannot be matched by `find`.

The default `auto` data type infers rendering from the value's type, not by inspecting string contents: a `Path` becomes an image (by suffix) or a file link, a `dict` or `list` becomes JSON, a DataFrame becomes a table, an `http(s)://` string becomes a link. Any other string stays `text`. numpy and pandas scalars, arrays, sets and datetimes are coerced to native Python with a warning; anything still unserialisable is rejected at the `add_meta` call.

## Deduplication surprises

With `dedup` on (the default), re-running a step with identical content (step type, parents, metadata and artifact bytes) returns the same node: the `with` block's variable is rebound onto the existing node and nothing new is stored. Change any of those and you get a distinct node. Failed runs never merge. For a record of every run regardless, create the store with `dedup=False`.

Read `node.node_id` after the block, not inside it. Rebinding happens on exit, so an id copied out during the block is provisional, and if the node then deduplicates into an existing one, that id is never persisted:

```python
with store.create_node(step_type="clean") as node:
    node.add_meta("rows", 100)
    stale = node.node_id      # provisional, may never exist
fresh = node.node_id          # the real id, after any rebinding
```

`store.get(stale)` returns `None`, and passing it as a `parent` raises `ValueError`. Passing the handle itself (`parent=node`) is always correct; if you need the string, read it after the block.

## Paths and artifacts

`artifacts(contains=...)` matches both as a glob and as a case-insensitive substring anywhere in the filename.

Artifacts must live inside the node. `node / "../outside.txt"` raises `ValueError`, and symlinks cannot get around it: at commit, links resolving outside the node are skipped with a `UserWarning` rather than followed. Links pointing at the node's own content are stored normally.

This matters most when copying a directory in wholesale. `shutil.copytree(src, node / "data", symlinks=True)` over a tree containing a link to a shared dataset would otherwise pull that file into the store. If you meant to store it, copy the file rather than the link.

## Automatic provenance

Every node records the OS user, Python version, platform, and the current git commit, branch and dirty state. The git fields come from two `git` subprocesses per node, run concurrently, so your identity and repository state are recorded by default. Provenance is display-only and not searchable. Outside a git repository, or without git installed, the git fields are `None`.

## Search semantics

A predicate passed to `find` receives `None` for any key the node lacks, so a blanket-true predicate such as `lambda v: True` matches every node, including ones missing that key. A predicate that raises is treated as no match, and warns.

`latest` ranks by stored creation time. Nodes created in the same instant tie, and which one is returned is arbitrary.
