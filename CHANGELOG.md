# Changelog

## [0.2.0] — 2026-07-08

The SQLite rebuild. A clean break from 0.1.x — every decision is recorded in `REBUILD_BLUEPRINT.md`.

### Changed
- The whole store is now **one SQLite database** (`<root>/ancestree.db`). Nodes are rows, not folders; metadata, the lineage DAG and the deduplicated artifact chunks all live in the same file, and every write is a real transaction. The hand-rolled index (snapshot + journal + reconcile), the temp-file rename dances, the background packer and its fork handling, the GC lock file — all deleted rather than ported.
- The API was redesigned into one vocabulary: `get_node`→`get`, `find_node`→`find`, `get_most_recent_node`→`latest`, `get_lineage`→`lineage`, `get_child_nodes`→`children`, `find_in_lineage`→`ancestors`. The renames are mechanical; `create_node`, `add_meta`, `artifacts()` and the `/` operator feel exactly as before.
- `create_node` now yields a **recording handle**; queries return an immutable, hashable **`Node` record**. Calling `add_meta` on a queried node — which used to silently do nothing useful — is no longer possible.
- `dedup` and `chunk` are **persisted store policy**, set once at creation like `rules`, instead of flags you had to remember to re-pass on every open.
- Structural facts (`step_type`, `generation`, `healthy`, `created_utc`, `duration_s`, `size_bytes`) are attributes on the record now, not metadata entries. `size_mb` became `size_bytes`; `timestamp` became `created_utc`.
- The static HTML export is a **view-only snapshot** (graph + click-to-view metadata). Searching moved to the live explorer, so the query grammar exists exactly once, compiled to SQL.

### Added
- `store.host_live_graph()` / `python -m ancestree serve` — a live explorer on localhost. Search (`field=value`, `accuracy>0.9`, free text), node diffs and the sortable runs table are all answered by SQL on the server.
- **Layer-2 deduplication**: near-identical artifacts are stored as chunk deltas against similar chunks already in the pool (zlib dictionary compression). 2.3× less storage on the near-duplicate benchmark for a 7% ingest cost — numbers in `benchmarks/RESULTS.md`.
- `store.sql(query)` — read-only SQL over a documented, versioned schema. The escape hatch for questions the API doesn't ask.
- `store.stats()` — node/chunk counts, logical vs stored bytes, and the dedup ratio.
- `store.export()` — grep-able per-node `meta.json` sidecars, whenever you want files you can read without ancestree.
- `store.compact()` — the one space-reclamation verb (drops unreferenced chunks, shrinks the database file). Replaces `gc()`/`flush()`/`clear_cache()`.
- Hard-kill recovery: a run killed mid-block leaves a seeded scratch directory, and the next store open adopts it as an unhealthy node. 0.1.x lost that work entirely.
- CLI: `python -m ancestree serve|export|compact <root>`.
- Four executed example notebooks: basic usage, a branching ML pipeline, a DAG stress test, and a CDC deep dive measuring save/load timings and storage savings per file type. The 0.1.x notebooks are kept under `docs/examples/legacy-0.1/`.
- Reads reassemble on demand: packed artifacts rebuild from the chunk pool into a session cache in the system temp dir, so the store root at rest is just the database.

### Removed
- **No migration from 0.1.x.** Old file-based stores stay on 0.1.x (it remains on PyPI).
- `Node.path` (nodes are not directories any more), `rebuild_db_from_disk()` (there is no separate index), `gc()`, `flush()`, `clear_cache()`.
- The NFS-safety claim. SQLite file locking over NFS is unreliable; keep stores on local disk.

## [0.1.0] — 2026-06-12

### Added
- `LineageStore` — core store for creating, searching, and managing pipeline nodes
- `Node` — represents a single pipeline step; holds artifacts and metadata on disk
- Rule enforcement — declare valid step-type transitions; invalid transitions raise immediately
- `gen_triggers` — declare which step types increment the generation counter
- Automatic provenance capture — user, platform, Python version, git commit, branch, and dirty-worktree flag recorded on every node
- Automatic timing and size capture — `duration_s` and `size_mb` recorded on every node
- Crash-safe context manager — failed nodes flagged `healthy=False`; empty nodes removed silently with a warning
- `add_meta()` — attach searchable, typed metadata to any node; supports `text`, `image`, `link`, `table`, `json`, and `code` types with auto-inference
- `get_node()` — resolve a node_id string into a Node object
- `find_node()` — search the store by metadata value or predicate
- `find_in_lineage()` — search within a node's ancestry
- `get_lineage()` — return a node's full ancestry, oldest first
- `get_most_recent_node()` — return the most recently created node matching a query
- `get_child_nodes()` — return direct descendants of a node
- `from_parent()` — shortcut to read artifacts from a node's parent
- `artifacts()` — list files inside a node's directory, with glob and substring filtering
- `prune()` — delete a node and its descendants, with dry-run support
- `rebuild_db_from_disk()` — recover and resync the search index from disk
- `generate_web_graph()` — render the entire store as a self-contained interactive HTML file
- Pipeline Explorer — lineage graph, metadata search, health indicators, colour-by-metric heatmap, runs table, activity timeline, node compare, and inline image/table rendering
- `lineage_database` — mtime-based, process-safe in-memory index with atomic snapshot replacement
- Zero dependencies — pure Python standard library throughout
- Full Google-style docstrings and MkDocs Material documentation site
- Jupyter notebook examples: basic usage, ML pipeline, 10k-node timing benchmark
