# Changelog

## [0.2.0] - 2026-07-25

The SQLite rebuild, and a clean break from 0.1.x. Every decision is recorded in `REBUILD_BLUEPRINT.md`.

### Changed
- The whole store is now one SQLite database (`<root>/ancestree.db`). Nodes are rows, not folders. Metadata, the lineage DAG and the deduplicated artifact chunks live in the same file, and every write is a transaction. The hand-rolled index (snapshot, journal and reconcile), the temp-file renames, the background packer and its fork handling, and the GC lock file were deleted rather than ported.
- The API was redesigned into one vocabulary: `get_node` to `get`, `find_node` to `find`, `get_most_recent_node` to `latest`, `get_lineage` to `lineage`, `get_child_nodes` to `children`, `find_in_lineage` to `ancestors`. The renames are mechanical. `create_node`, `add_meta`, `artifacts()` and the `/` operator are unchanged.
- The three output methods now name themselves by what they emit and how: `generate_web_graph()` to `export_graph()` (static HTML snapshot), `host_live_graph()` to `serve_graph()` (the live explorer), and `export()` to `export_metadata()` (the `meta.json` sidecars). `export()` claimed the generic word while all three are exports; the pairing also matches the `serve`/`export` CLI commands.
- `create_node` yields a recording handle, and queries return an immutable, hashable `Node` record. Calling `add_meta` on a queried node, which previously did nothing useful, is no longer possible.
- `reuse_identical` (formerly `dedup`) and `delta` (formerly `chunk`) are persisted store policy, set once at creation like `rules`, rather than flags to re-pass on every open. The names now say what they do: `reuse_identical` binds a rerun with identical content onto the existing node, and `delta` gates Layer-2 delta storage. Layer-1 chunking and exact chunk deduplication always run — the old `chunk=False` never disabled chunking.
- Structural facts (`step_type`, `generation`, `healthy`, `created_utc`, `duration_seconds`, `created_epoch_seconds`, `size_bytes`) are attributes on the record, not metadata entries. `size_mb` became `size_bytes`, `timestamp` became `created_utc`, and `duration_s` became `duration_seconds` — every unit is now spelled out in the name.
- The static HTML export is a view-only snapshot: the graph plus click-to-view metadata. Searching moved to the live explorer, so the query grammar exists once and compiles to SQL.

### Added
- `store.serve_graph()` and `python -m ancestree serve`: a live explorer on localhost. Search (`field=value`, `accuracy>0.9`, free text), node diffs and the sortable runs table are answered by SQL on the server.
- Layer-2 deduplication: near-identical artifacts are stored as chunk deltas against similar chunks already in the pool, using zlib dictionary compression. On a mixed 69 MB corpus of six file types across six revisions the store is 2.63x smaller, and 2.23x on the synthetic near-duplicate benchmark. Numbers are in `benchmarks/RESULTS.md`. Payloads zlib cannot shrink (PNG, parquet, zip) are stored verbatim rather than re-compressed.
- `store.sql(query)`: read-only SQL over a documented, versioned schema, for questions the API does not cover.
- `store.stats()`: node and chunk counts, `artifact_bytes` versus `chunk_stored_bytes`, and the dedup ratio. `database_bytes` counts the WAL alongside the database, so it reflects what the store occupies.
- `store.export_metadata()`: per-node `meta.json` sidecars, for reading a store without ancestree installed.
- `store.compact()`: drops unreferenced chunks and shrinks the database file, replacing `gc()`, `flush()` and `clear_cache()`. It is rarely needed directly, since `prune(node, dry_run=False)` compacts for you and `compact=False` defers it when pruning in a loop.
- `store.backup(dest)`: a consistent copy via SQLite's online backup API, safe to take while the store is open and being written. Copying `ancestree.db` alone out from under a live store silently yields an empty store.
- Hard-kill recovery: a run killed mid-block leaves a seeded scratch directory, and the next store open adopts it as an unhealthy node. 0.1.x lost that work.
- CLI: `python -m ancestree serve|export|compact <root>`.
- Two executed example notebooks: basic usage and a branching ML pipeline. The 0.1.x notebooks are kept under `docs/examples/legacy-0.1/`, and the chunking and timing measurements are in `benchmarks/RESULTS.md`.
- Reads reassemble on demand: packed artifacts rebuild from the chunk pool into a per-session cache at `<root>/.cache/`, cleared when the session ends. The live explorer serves artifacts straight from the chunk pool and never touches that cache.
- Artifact containment is enforced against symlinks as well as paths. `node / "../outside.txt"` has always raised, but a symlink pointing the same way used to be followed at commit and its target stored as a node artifact, reachable unintentionally via `shutil.copytree(..., symlinks=True)` over a tree containing a link to a shared file. Escaping links are now skipped with a warning, and links resolving inside the node are stored normally.

### Performance
- The average chunk size is 16 KiB, not 32 KiB. At 32 KiB a delta base sat exactly on zlib's 32,256-byte dictionary window, so it could not be seen in full and the tail of every delta degenerated to literals. Halving it stores 14% less and ingests 22% faster. The gain is free because the chunker skips `MIN_SIZE` bytes per chunk, so smaller chunks mean more bytes skipped.
- Node creation is about 2.2x faster for metadata-only nodes. Provenance capture was 60% of the cost: three `git` subprocesses per node became two, run concurrently.
- The chunker's hot loop runs at 26.8 MB/s, up from 16.6, by narrowing it to the bits the boundary test reads. Boundaries are bit-for-bit identical.
- `find`, `lineage` and `ancestors` batch their id lookups instead of issuing two queries per node, and `idx_meta_key` covers `(key, value)`. A selective `find()` over 3000 nodes went from 0.40 ms to 0.04 ms. The live explorer's page render no longer costs four queries per node.

### Removed
- No migration, from 0.1.x or between any two versions. A store records its format version in the database at creation, and ancestree checks it on open and refuses anything it did not write, leaving the file untouched. Table names cannot stand in for this: the chunk encoding can change without the schema's shape changing. A 0.1.x store predates the database (a directory per node plus `.lineage_config.json`) and is refused on that layout rather than reopened as an empty store beside the old nodes. Converting stores is not a goal; keep the version that wrote a store installed to read it. 0.1.x remains on PyPI.
- `Node.path` (nodes are not directories), `rebuild_db_from_disk()` (there is no separate index), `gc()`, `flush()` and `clear_cache()`.
- The NFS-safety claim. SQLite file locking over NFS is unreliable, so keep stores on local disk.

## [0.1.0] - 2026-06-12

### Added
- `LineageStore`: core store for creating, searching, and managing pipeline nodes
- `Node`: represents a single pipeline step; holds artifacts and metadata on disk
- Rule enforcement: declare valid step-type transitions; invalid transitions raise immediately
- `gen_triggers`: declare which step types increment the generation counter
- Automatic provenance capture: user, platform, Python version, git commit, branch, and dirty-worktree flag recorded on every node
- Automatic timing and size capture: `duration_s` and `size_mb` recorded on every node
- Crash-safe context manager: failed nodes flagged `healthy=False`; empty nodes removed silently with a warning
- `add_meta()`: attach searchable, typed metadata to any node; supports `text`, `image`, `link`, `table`, `json`, and `code` types with auto-inference
- `get_node()`: resolve a node_id string into a Node object
- `find_node()`: search the store by metadata value or predicate
- `find_in_lineage()`: search within a node's ancestry
- `get_lineage()`: return a node's full ancestry, oldest first
- `get_most_recent_node()`: return the most recently created node matching a query
- `get_child_nodes()`: return direct descendants of a node
- `from_parent()`: shortcut to read artifacts from a node's parent
- `artifacts()`: list files inside a node's directory, with glob and substring filtering
- `prune()`: delete a node and its descendants, with dry-run support
- `rebuild_db_from_disk()`: recover and resync the search index from disk
- `generate_web_graph()`: render the entire store as a self-contained interactive HTML file
- Pipeline Explorer: lineage graph, metadata search, health indicators, colour-by-metric heatmap, runs table, activity timeline, node compare, and inline image/table rendering
- `lineage_database`: mtime-based, process-safe in-memory index with atomic snapshot replacement
- Zero dependencies: pure Python standard library throughout
- Full Google-style docstrings and MkDocs Material documentation site
- Jupyter notebook examples: basic usage, ML pipeline, 10k-node timing benchmark
