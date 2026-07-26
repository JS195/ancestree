# Ancestree Rebuild Blueprint & Action Plan

> **Version:** 2.5 &nbsp;•&nbsp; **Date:** 2026-07-08 &nbsp;•&nbsp; **Author:** Joshua Smith &nbsp;•&nbsp; **Status:** In progress

This is my working plan for rebuilding ancestree on a single SQLite backing store: metadata and deduplicated chunks in one database, a proper two-layer dedup scheme, the two big classes broken up, and a locally hosted interactive explorer — all without adding a single dependency.

**How I use this file.** It is the record of why I made each call and the checklist of how the work lands. I tick the [roadmap](#10-implementation-roadmap) as phases merge, and when I change my mind about something I update the relevant entry in [§4](#4-key-architectural-decisions) and add a line to the [decision log](#decision-log). If this file and the code disagree, one of them is wrong and I fix it.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals, Non-Goals & Hard Constraints](#2-goals-non-goals--hard-constraints)
3. [Current-State Assessment & Redundancy Audit](#3-current-state-assessment--redundancy-audit)
4. [Key Architectural Decisions](#4-key-architectural-decisions)
5. [Target Architecture & Directory Layout](#5-target-architecture--directory-layout)
6. [Data Model — SQLite Schema](#6-data-model--sqlite-schema)
7. [Runtime Behaviour & Data Flow](#7-runtime-behaviour--data-flow)
8. [What Gets Deleted — The Simplification Payoff](#8-what-gets-deleted--the-simplification-payoff)
9. [Trade-offs & Risks](#9-trade-offs--risks)
10. [Implementation Roadmap](#10-implementation-roadmap)
11. [Public API](#11-public-api)
12. [Future Work](#12-future-work)
13. [Glossary](#13-glossary)
14. [Decision Log](#decision-log)

---

## 1. Executive Summary

Ancestree is my zero-dependency, pure-stdlib lineage tracker. Every step of a pipeline is a node; nodes chain into a DAG that is durable on disk, queryable from Python, and viewable as a self-contained interactive HTML file.

The 0.1.x design was a tree of directories: one folder per node holding a `meta.json`, an artifact manifest and the artifact files, backed by a hand-rolled JSON index (snapshot + append-only journal + directory reconcile) and an on-disk chunk pool.

This rebuild moves **all durable state into one SQLite database (`ancestree.db`)** — metadata and deduplicated chunks together — whilst keeping the package dependency-free (`sqlite3` and `http.server` are standard library). What that buys me:

- **Roughly 40% of the codebase gets deleted rather than moved** — the snapshot/journal/reconcile index, all the temp-file rename dances, the loose-vs-packed reclaim ordering, the straggler scans and lock files. SQLite transactions do that job now.
- **Nodes become rows, not folders.** A short-lived scratch directory exists only while a node's artifacts are being written, then it is ingested and deleted.
- **Deduplication gets a second layer**: FastCDC (variable, content-defined chunking) plus resemblance-matched delta storage via zlib dictionary compression, which catches near-duplicate blocks that exact chunking cannot.
- **The two big classes** (`LineageStore`, 899 lines; `Node`, 716 lines) get broken into focused, testable units.
- **A locally hosted live explorer** (`serve_graph()`, stdlib `http.server`) joins the emailable single-file HTML export instead of replacing it.

The result: faster loading (memory-mapped, indexed queries instead of an O(N) directory reconcile on every cold start), native-speed writes (user code still just writes files), far simpler persistence, better storage efficiency — no new dependencies, and a public API I have deliberately redesigned rather than preserved (see [§11](#11-public-api)).

This is a **consolidation onto a better substrate**, not a rescue of bad code. Good existing logic will be preserved, not rewritten.

---

## 2. Goals, Non-Goals & Hard Constraints

### Goals

| # | Goal | Why |
|---|------|-----|
| G1 | Single SQLite backing store for metadata **and** chunks | Proper querying, complex datatypes, one portable file, atomic durability |
| G2 | Advanced deduplication (variable CDC + near-duplicate delta) | Maximise storage efficiency; find repeated blocks beyond exact matches |
| G3 | Break `LineageStore` and `Node` into focused classes | The two God-classes are hard to reason about |
| G4 | Optional local live explorer | An always-current graph during a run, no rebuild step |
| G5 | Simpler, native-speed persistence & loading | Get rid of the loose logic trailing around; fast cold start |
| G6 | A coherent, deliberately-designed public API | The point of the rebuild; compatibility with 0.1.x is not required |

### Non-Goals (this phase)

- **DataFrame SQL querying** — storing pandas DataFrames as real relational tables and querying into their contents. Parked in [Future Work](#12-future-work); the schema leaves room for it.
- **A networked / multi-user server** — the live server binds to `127.0.0.1` only. No auth, no remote hosting.
- **NFS safety** — SQLite file locking over NFS is unreliable, so the old file-based guarantee is deliberately relaxed (see [§9](#9-trade-offs--risks)).

### Hard Constraints (non-negotiable)

- **HC1 — Zero dependencies.** `pip install ancestree-track` pulls nothing but the standard library. That rules out Postgres/MySQL, Flask/FastAPI, zstd/bsdiff/xdelta, and pandas-as-a-requirement. `sqlite3`, `http.server`, `zlib`, `hashlib` are all stdlib and fine.
- **HC2 — Keep the read/write ergonomics.** `node / "file.csv"` gives a real path to write to; `node.artifacts()` gives readable paths back. Nobody should have to learn new syntax.
- **HC3 — Ergonomics over compatibility.** Backwards compatibility is explicitly *not* required (the package is pre-1.0). I am free to change the API wherever that improves the design; the only fixed points are the feel of writing/reading artifacts (HC2) and the zero-dependency rule (HC1). The redesign is specified in [§11](#11-public-api) and [AD10](#ad10--redesign-the-public-api-for-coherence).

---

## 3. Current-State Assessment & Redundancy Audit

### 3.1 Modules today

| Module | Lines | Responsibility |
|--------|-------|----------------|
| `core.py` | 899 | `LineageStore` — config, node creation, rules, node reuse, background packer, querying, prune, GC, viz |
| `models.py` | 716 | `Node` — metadata envelopes, content hashing, chunk packing/reassembly, manifest, path resolution |
| `database.py` | 286 | `lineage_database` — JSON index: snapshot + append-only journal + directory reconcile |
| `chunkstore.py` | 257 | FastCDC chunker, content-addressed chunk pool, session read cache |
| `utils.py` | 218 | Metadata matching, JSON coercion, provenance capture, time parsing |
| `vis.py` | 140 | Build graph payload; inline assets into one HTML file |
| `assets/` | ~2200 | `actions.js`, `styles.css`, `template_new.html`, `vis-network.min.js` |

### 3.2 Redundancies, anti-patterns & loose logic

The real problem is not the storage medium — it is all the crash-ordering scaffolding a filesystem forces on you because it has no transactions. Concretely:

- **Hand-rolled atomicity, five times over.** `meta.json`, `.artifacts.json`, chunk files, `.index.json` and the config file each do their own `*.tmp` + atomic-`replace()` dance with unique temp names to survive concurrent writers.
- **Deferred-packing state sprawl.** `LineageStore` carries seven pieces of worker state (`_pack_queue`, `_pack_worker`, `_pack_lock`, `_enqueued`, `_reclaim_pending`, `_flush_registered`, `_scan_done`) plus a module-level `WeakSet` and an `os.register_at_fork` handler, all to keep one background chunker crash- and fork-safe.
- **Loose-vs-packed reclaim ordering.** Artifacts sit as loose files "until their recipe is durable", then get reclaimed at a quiescent point — plus a straggler scan to recover from crashes.
- **A disposable index that has to reconcile against disk.** `_reconcile`, `_replay_log`, `_write_snapshot`, `_is_stale`, `_refresh_if_stale`, `rebuild_from_disk` — and an O(N) `iterdir()` on every cold start.
- **The same directory walk in five places.** The `rglob("*")` + skip-`meta.json`/`.artifacts.json`/`.tmp` filter appears in five different methods.
- **System-vs-user metadata held apart by convention.** Three overlapping key-sets plus a `_system_keys` snapshot keep structural, provenance and user metadata separate inside one flat dict.
- **Read-cache reaping machinery.** `fcntl` per-session lock files and a reap-dead-sessions scan to clean up after crashed sessions.
- **Three Node constructors + lazy hydrate.** `_create` / `_load` / `_from_index` + `_hydrate`.
- **Naming and doc drift.** `lineage_database` is lowercase and is a JSON index, not a database; `create_node`'s signature says `dedupe=True, chunk=True` whilst its docstring says both default to False; docstrings reference a `compact()` that does not exist; there are two time parsers.
- **Fragile web templating.** `vis.py` inlines assets by replacing exact strings like `<script src="../../web_app/vis-network.min.js"></script>`.

### 3.3 What is already good (keep, don't rewrite)

- **FastCDC** (`chunkstore.py`) — a correct normalised-chunking implementation with a fixed Gear table.
- **Node content fingerprinting** (`content_hash`, `_content_equal`) — the node-level dedup work from my feature branch.
- **Provenance capture** — user / Python / platform / git commit / branch / dirty flag.
- **The crash-forensic semantics** — partial output from a failed step is kept and flagged `healthy=False`.
- **The visual explorer** — lineage graph, search, diff, dark mode, runs table.

---

## 4. Key Architectural Decisions

Each decision records what I chose, why, and what it costs. Everything in [§5](#5-target-architecture--directory-layout)–[§7](#7-runtime-behaviour--data-flow) follows from these.

### AD1 — SQLite is the single source of truth (metadata **and** chunks)
- **Decision.** All durable state lives in one `ancestree.db` (WAL mode). The JSON index, the config file, per-node `meta.json`/manifest and the `.chunks/` pool all become tables.
- **Why.** Real querying, complex datatypes, indexed lookups, and — the big one — transactional atomicity for free, which deletes the whole hand-rolled crash-ordering layer. `sqlite3` is stdlib, so HC1 holds. Keeping chunks and metadata in **one transactional domain** matters in itself: a half-migration (metadata in SQL, chunks on the filesystem) would reintroduce exactly the two-phase crash-ordering problem I am deleting.
- **Cost.** Loses the "files are the database / grep it / rebuild from disk" safety net. See [AD9](#ad9--accept-single-db-as-truth-resilience-posture).

### AD2 — Nodes are rows, not folders
- **Decision.** A node is a row in the `node` table. A transient `.scratch/<node_id>/` directory exists only during its `create_node` block, gets ingested into SQLite at block exit, and is deleted.
- **Why.** Once SQLite owns metadata and chunks, the durable per-node directory is vestigial. This deletes `Node.path`, the five `rglob` walks, the loose-vs-packed resolution, and turns `prune` into a SQL cascade.
- **Cost.** A scratch workspace still has to exist because pandas, PIL and friends need a real path to write to (HC2). "No folders" means no *durable* folders.

### AD3 — SQLite transactions own atomicity
- **Decision.** Every write is a transaction. All the `*.tmp`+`replace()` dances, the `.gc.lock`, the loose-file reclaim ordering and the manifest swap go.
- **Why.** This is where most of the loose logic lives, and it only exists because a filesystem has no transactions. SQLite has them.
- **Cost.** Nothing material. Chunk ingest becomes one atomic unit — blobs and artifact rows commit together or not at all.

### AD4 — Synchronous CDC by default; background is an opt-in flag
- **Decision.** Chunking runs synchronously at block exit, after the user's code. A `background=True` seam exists but stays off.
- **Why.** The user's write path is already native speed (plain files into scratch); chunking happens after their code has finished. Synchronous deletes the worker thread, queue, locks, fork handler and straggler scan outright, and it means "block closed = saved and visible to other processes".
- **Cost.** Block exit blocks for the chunking time. The per-byte Gear loop is pure Python — realistically **~3–10 MB/s** (SHA-256 and zlib are C and not the bottleneck) — so this is imperceptible for KB–MB nodes but grows to tens of seconds past 100 MB. Two mitigations: **(1) the large-artifact fallback** — files above a threshold (default ~64 MB) skip the Gear loop and chunk at fixed max-size boundaries, keeping ingest at C speed (whole-file dedup kept; only insert-shift resilience lost, which matters least for huge binaries); **(2)** the `background=True` seam. I would only flip to background if all three hold: nodes routinely write 100 MB+ *and* the fallback's dedup trade is unacceptable, the pause is actually observed, and there is GIL-releasing work between writes for a worker to hide behind. Background hides latency, it does not create speed — total work is identical.

### AD5 — Two-layer deduplication
- **Decision.** *Layer 1:* FastCDC → byte-identical chunks stored once. *Layer 2:* derive min-wise super-features in the same pass as chunking, look up similar-but-not-identical stored chunks, and store the newcomer as a **delta**: zlib dictionary compression with the base chunk as the preset dictionary (`zlib.compressobj(zdict=base)`), kept only when it is genuinely smaller than raw. **Delta depth is capped at 1 — a base is always a raw chunk** — so a read costs at most two fetches and GC reachability is a single hop.
- **Why.** This catches the near-duplicate blocks exact CDC structurally misses — the actual "find repeated blocks" win. Using DEFLATE's dictionary mechanism instead of writing my own copy/insert codec keeps the codec **C-speed and about 20 lines** (encode = compress with `zdict`, decode = decompress with `zdict`), instead of what would otherwise have been the slowest pure-Python code in the build. Stdlib only (HC1).
- **Limits & gate.** DEFLATE's 32 KB window means a base contributes at most ~32 KB of reference data — full coverage at my 32 KB average chunk size, partial for bigger chunks. Layer 2 ships behind the `delta` policy and its **default was decided by a benchmark, not by faith** (Phase 6): measured dedup ratio vs ingest/read overhead on realistic near-duplicate fixtures.
- **Cost.** Reading a delta chunk fetches its base too (bounded by depth 1); the resemblance index adds one lookup plus a few rows per new chunk; `compact()` has to keep live bases (a one-hop closure).

### AD6 — Structural & provenance fields become columns
- **Decision.** `step_type`, `generation`, `timestamp`, `healthy`, `duration_seconds`, `size`, `content_hash` and the provenance fields become real columns on `node`. The `metadata` table holds **only** user metadata.
- **Why.** The system-vs-user distinction becomes schema, enforced by the database, instead of three overlapping key-sets maintained by convention.
- **Cost.** Nothing material.

### AD7 — Search is SQL; predicates fall back to Python
- **Decision.** `find` compiles to `WHERE` clauses (equality plus numeric ranges via a `num_value` column). Lineage is a recursive CTE; children are an indexed edge lookup. Callable predicates (lambdas) run in Python over the SQL-narrowed candidates.
- **Why.** Collapses `flatten_meta`, `to_db` and most of `is_match`, and scales past linear scans. Lambdas stay because they are one of the nicest parts of the API.

### AD8 — Keep the `/` ergonomics and the static export; add the server
- **Decision.** `node / "x"` and `node.artifacts()` keep their feel (HC2). `export_graph()` still writes an emailable single-file HTML, now a **view-only snapshot** (graph + click-to-view metadata, no search box — see AD11). `serve_graph()` is **added** as the searchable, always-current explorer.
- **Why.** I keep a shareable offline artifact whilst the rich exploring moves to the server.

### AD9 — Accept single-DB-as-truth resilience posture
- **Decision.** A corrupt `ancestree.db` is real data loss with no filesystem fallback. The recovery story is: WAL + `PRAGMA integrity_check` + backups + `export()` writing grep-able `meta.json` sidecars on demand.
- **Why.** This is the price of AD1–AD3's simplicity. Writing it down so it is a choice, not a surprise.

### AD10 — Redesign the public API for coherence
- **Decision.** Backwards compatibility is dropped (pre-1.0). The API gets one consistent vocabulary and sheds surface that stops meaning anything after the rebuild.
- **Removed.** `Node.path` (nodes are rows); `rebuild_db_from_disk()` (no separate index to rebuild); `flush()`/`clear_cache()` (lifecycle is automatic; space is reclaimed by `compact()`).
- **Renamed** into one query vocabulary: `get_node`→`get`, `find_node`→`find`, `get_most_recent_node`→`latest`, `get_child_nodes`→`children`, `get_lineage`→`lineage`, `find_in_lineage`→`ancestors`.
- **Reshaped.** `reuse_identical`/`delta` become persisted store policy set once at creation (like `rules`), not per-open flags; maintenance converges on `compact()`; visualisation is `export_graph()` (static) + `serve_graph()` (live).
- **Restructured.** The mutable **recording handle** yielded by `create_node` (write API: `/`, `add_meta`) is split from the immutable **`Node` record** returned by queries (read API: attributes, `metadata`, `artifacts`). This kills the old footgun of calling `add_meta` on a queried node and lets the record be a proper hashable value object.
- **Why.** The old verbs mixed `get_*`/`find_*` inconsistently and several methods stop making sense post-rebuild. A coherent API is one of the goals, not a casualty.
- **Cost.** A breaking change for 0.1.x, which I am fine with pre-1.0. The test suite gets rewritten to the new API rather than pinned to the old one.

### AD11 — The live server queries the database directly; no client-side "grep"
- **Decision.** In `serve_graph`, all search, filtering, lineage highlighting, node detail and diff are answered by **the server running SQL** against `ancestree.db`. The browser is a thin client: it fetches a lightweight graph skeleton for layout and asks the server for everything else. No metadata blob gets shipped to the browser, and no query engine runs there.
- **Why.** Metadata lives in SQL now. Re-inlining it all and re-implementing a query language in JavaScript would duplicate the Python query engine and does not scale — 10k annotated nodes is a multi-MB blob. One query grammar (Python → SQL) serves both `store.find(...)` and `/api/search`.
- **Static export.** An offline single file has no server to ask, so rather than keep a second query implementation in JS, the static export is a **view-only snapshot**: graph plus click-to-view metadata, no search box. Exactly one query implementation exists as a result.
- **Cost.** Rich search needs the server running (which is the point of live mode). The emailable file becomes a shareable snapshot rather than a search tool.
- **Amended at v2.7.** Live mode now serves the **classic 0.1.x explorer** (`template_new.html` + `styles.css` + `actions.js`) with the whole store baked in as `window.PIPELINE_DATA`, re-rendered on every page load — I missed the old front end, and refreshing the page beats wiring a thin client for a local tool. Its search/diff/runs table run client-side in the template's JS; the SQL-backed JSON API stays for programmatic use, so the *Python* query grammar remains the single one the library itself exposes. The static export is still the view-only snapshot.

---

## 5. Target Architecture & Directory Layout

### 5.1 Package layout

```
src/ancestree/
├── __init__.py               # public API surface (LineageStore, Node, __version__)
├── __main__.py               # CLI: python -m ancestree serve|export|compact
├── py.typed
├── assets/                   # the classic explorer, served live (template · styles · actions · vis)
├── store.py                  # LineageStore — thin facade that wires the layers
├── maintenance.py            # Pruner + compact (chunk GC + incremental_vacuum) + orphan-scratch sweep
├── errors.py                 # typed exception hierarchy (cross-cutting)
├── util.py                   # JSON coercion, ISO time, small shared helpers (cross-cutting)
│
├── domain/                   # what a node IS — pure logic, no I/O
│   ├── __init__.py
│   ├── node.py               # Node record + recording handle
│   ├── metadata.py           # metadata envelope: validate / coerce / infer type
│   ├── rules.py              # RuleEngine — transition validation + generation numbering
│   ├── fingerprint.py        # node content-identity (hash + equality) → reuse_identical
│   └── provenance.py         # who/what/how capture (user, python, platform, git)
│
├── db/                       # how state is PERSISTED — all SQLite
│   ├── __init__.py
│   ├── connection.py         # ConnectionManager: pragmas, per-thread/PID conns, write lock, fork reset
│   ├── schema.py             # canonical DDL + ensure/verify against user_version
│   ├── metadata_store.py     # MetadataStore: node/edge/metadata rows, queries, lineage
│   └── chunk_store.py        # ChunkStore: chunk & delta BLOBs, artifacts, reassembly, read cache, gc
│
├── ingest/                   # how bytes GET IN — the write path
│   ├── __init__.py
│   ├── workspace.py          # NodeWorkspace — scratch dirs; the ONLY filesystem writer
│   ├── cdc.py                # CDC in one module: FastCDC (L1) · super-features · zlib-zdict delta (L2)
│   └── packing.py            # ingest pipeline: scratch files → chunk → (delta?) → SQLite
│
└── web/                      # how it's SEEN
    ├── __init__.py
    ├── graph.py              # build {nodes, edges, levels} payload from the store
    ├── export.py             # single-file static HTML export (+ artifact materialization)
    ├── server.py             # local live server (http.server) + JSON API + on-demand artifacts
    └── assets/
        ├── static.html
        └── vis-network.min.js
```

The root holds only entry points (`store`, `__main__`) and cross-cutting leaves (`maintenance`, `errors`, `util`); each package is one architectural layer. The 0.1.x modules used to sit flat beside these during the transition — they were deleted after Phase 7 once compatibility was dropped (v2.5).

### 5.2 On-disk layout (at rest)

```
<root>/
  ancestree.db (+ -wal, -shm)   # the entire durable store
  .scratch/<node_id>/…          # exists only mid-write, then ingested and deleted
  .cache/<pid>-<suffix>/…       # session read cache: reassembled artifact copies
  interactive_pipeline.html     # static export, on demand
```

The reassembled-artifact **read cache** lives at `<root>/.cache/<session>/` — inside the store, so the paths reads hand back make sense at a glance (v2.6; it sat in the system temp dir from v2.1 until the sweep made that unnecessary). Sessions are pid-tagged, cleaned on close/exit, and the store-open sweep reaps any whose owner died — it holds nothing but derived data, so reaping can never lose work. Reads never resolve into `.scratch` itself: the sweep adopts orphaned scratch as unhealthy *nodes*, so cache copies must not live there.

### 5.3 File-by-file — contents & rationale

#### Top level — entry points & cross-cutting leaves

**`__init__.py`** — re-exports `LineageStore` and `Node`. One stable import point that hides the internal layout.

**`__main__.py`** — an `argparse` CLI: `python -m ancestree serve|export|compact <root>`. The live server and the maintenance verbs should be usable without writing Python; stdlib only (HC1).

**`store.py` — `LineageStore` (facade).** Construction and wiring of `ConnectionManager`, `MetadataStore`, `ChunkStore`, `RuleEngine`, `Pruner`; the context manager; and thin delegating methods (`create_node`, `get`/`find`/`latest`/`lineage`/`children`/`ancestors`, `prune`, `compact`, `sql`, `stats`, `export_metadata`, `export_graph`, `serve_graph`). The old 899-line class did everything itself; this one only orchestrates — every algorithm lives in a focused module.

**`maintenance.py`.** `Pruner.prune(node_id, dry_run=True)` (DAG-aware: a child dies only if all its parents die — expressed with SQL + `ON DELETE CASCADE`); `compact()` (delete chunks nothing references — a one-hop closure under depth-1 — then `PRAGMA incremental_vacuum`, with full `VACUUM` as the deep option); and the **orphan-scratch sweep** run at store open (adopt a dead process's seeded scratch as an unhealthy node — §7.5 — and reap dead sessions' read-cache directories). Destructive and space ops kept away from the read/write path; top-level because they cut across `db/` and `ingest/`. One verb (`compact`) replaces the old `gc`/`flush`/`clear_cache` trio.

**`errors.py`.** `AncestreeError` base plus `InvalidTransition`, `NodeNotFound`, `ArtifactNotFound`, `SchemaError`, `IntegrityError`, `CorruptChunkError` — so callers can catch selectively instead of fishing bare `ValueError`s out of library code.

**`util.py`.** `to_jsonable` (duck-typed numpy/pandas coercion — both stay optional), ISO time parse/format in one place, and `filter_relpaths` (the artifact-name matcher every `artifacts()` surface shares).

#### `domain/` — the vocabulary of lineage (pure logic, no I/O)

**`domain/node.py` — `Node` (record) + recording handle.** The immutable **`Node`** queries return (identity + `metadata` property, `artifacts`, read-side `/`, `__repr__`), and the mutable **recording handle** `create_node` yields (write-side `/` and `add_meta`). Both read through the store. Splitting them stops anyone calling `add_meta` on a queried node and makes the record a hashable value object ([AD10](#ad10--redesign-the-public-api-for-coherence)). A node is a row, not a directory.

**`domain/metadata.py`.** The `{value, data_type, group, searchable}` envelope builder — validation, reserved-key guard, `auto` type inference, DataFrame → `{columns, rows}` coercion, and the serialisability check that fails loudly at the `add_meta` call site instead of at block exit where the traceback would point at nothing useful.

**`domain/rules.py` — `RuleEngine`.** `validate(step_type, parent_step_types)` (raises `InvalidTransition`) and `generation_for(...)`. Pure and unit-testable, lifted out of `create_node`.

**`domain/fingerprint.py`.** `ContentSummary` — step_type, order-independent parents, metadata envelopes, artifact SHA-256s — with its digest. Content identity is its own thing, not node behaviour, so it gets its own module and its own tests.

**`domain/provenance.py`.** `capture()` → user / python_version / platform / git_commit / git_branch / git_dirty. Best-effort; a machine without git must never break node creation.

#### `db/` — persistence

**`connection.py` — `ConnectionManager`.** Opens the DB with `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout`, `mmap_size`, `temp_store=MEMORY`; hands out thread-local read connections; exposes a serialised, **reentrant** `write()` transaction (single writer; nested blocks join the outermost transaction, so ingest commits node row + chunks + artifacts as one atomic unit); rebinds connections if the PID changed (fork safety); checkpoints the WAL after large ingests so the sidecar never balloons. SQLite's threading/fork rules are the trickiest correctness surface in the build, so exactly one module owns them.

**`schema.py`.** `SCHEMA_SQL`, `SCHEMA_VERSION`, `ensure_schema(conn)` (stamps `PRAGMA user_version` at creation, verifies it on every open, then checks the expected tables are present). The DDL lives in one canonical, versioned place; `auto_vacuum=INCREMENTAL` is set at creation because it cannot be enabled later. There is no migration: a store at any other version is refused, never converted. The stamp is what makes that check possible — table names alone would not catch a change to the chunk encoding or to the meaning of an existing column.

**`metadata_store.py` — `MetadataStore`.** `add_node` (node row + edges + metadata rows in one transaction), `get`, `exists`, `find(**kwargs)`, `most_recent`, `children`, `lineage` (recursive CTE), `find_by_hash`, `remove`, `all_node_ids`. Replaces the old `lineage_database` and every bit of its snapshot/journal/reconcile machinery. Multi-key `find` intersects per-key subqueries on the indexed metadata table — the trickiest SQL in the build, deliberately confined to this one well-tested method. `find(parent_id=…)` matches the node's ordered parent list from the edge table (an empty list matches roots).

**`chunk_store.py` — `ChunkStore`.** `put_chunk` (exact dedup first — a pooled chunk costs nothing — then, with the `chunk` policy on, super-features nominate the most similar **raw** base and a trial delta is kept only when it genuinely beats plain compression); `get_chunk` (decode and digest-verify, fetching a delta's raw base — never more than two fetches); artifact recipes; `reassemble` into the session **read cache** (`<root>/.cache/<pid>-<suffix>/` — pid-tagged so the store-open sweep can reap a dead session's leftovers; the old `fcntl` lock/reap machinery stays gone).

#### `ingest/` — the write path (scratch → chunks → SQLite)

**`ingest/workspace.py` — `NodeWorkspace`.** Creates the scratch dir, resolves `node / "x"` write targets (escape-guarded), enumerates written files, and drives ingest → delete at block exit. Writes a small **seed file** (node_id, step_type, parents, generation, pid) at block start so a hard-killed block's scratch can be adopted later (§7.5). The *only* module that writes the filesystem.

**`ingest/cdc.py`.** Three clearly-sectioned parts in one module: **(1) the FastCDC chunker** — Gear table, normalised masks, retunable constants, plus the fixed-boundary fallback for huge files (AD4); **(2) resemblance** — min-wise super-features over strided samples, backing the `chunk_feature` lookup; **(3) the delta codec** — `encode`/`decode` via zlib dictionary compression (AD5). Three faces of one algorithmic concern; a three-file package would just fragment it.

**`ingest/packing.py`.** `ingest_node`: read the scratch files once, chunk, dedup, and commit node row + edges + metadata + chunks + artifact recipes as **one transaction**. On failure everything rolls back and the scratch survives. Computes artifact SHA-256s and the node's size in the same pass (node-level dedup needs them). Synchronous by default; the same entry point could be dispatched to a worker if `background=True` ever earns its keep.

#### `web/`

**`graph.py`.** `build_graph(store)` — the lightweight skeleton (ids, labels, step-type groups, levels, edges) for layout — and `node_detail(store, id)` — one node's full record. The static export inlines every detail once; the live server serves the same shape on demand, which keeps its payload small (AD11).

**`export.py`.** `export_static(store, dest)` — the **view-only** single-file HTML (graph + click-to-view metadata, no search box; AD11), rendered through validated named markers (each must appear exactly once or the export fails loudly — no more brittle exact-string tag matching). Artifact bytes live in SQLite now, so references get materialised at export time: small images inline as data URIs (the common case stays genuinely single-file), everything else lands beside the file under `<name>_files/`, and `include_artifacts=False` gives a metadata-only snapshot. The embedded JSON escapes `</` so no metadata value can break out of its script tag.

**`server.py`.** The live explorer: `http.server` on `127.0.0.1` serving the **classic 0.1.x front end** (`template_new.html` with `styles.css`, `actions.js` and vis-network inlined at the template's original markers), the store baked in as `window.PIPELINE_DATA` and re-rendered on every page load — refresh the browser and nodes created since the server started appear. Artifact links resolve at `/<node_id>/<relpath>`, and the SQL-backed JSON API (`/api/graph`, `/api/node`, `/api/search`, `/api/diff`, `/api/runs`, `/api/artifact`) stays for programmatic use. **Every endpoint resolves by database key, never a filesystem path — traversal is structurally impossible.** Deliberately single-threaded: one SQLite read connection, trivial lifecycle. `serve_graph` defaults to non-blocking + opening the browser, and re-running it replaces the previous server (notebook-friendly); the CLI passes `block=True`.

**`web/assets/`** holds `static.html` (the view-only snapshot) and vis-network; the classic explorer's assets live at the package root (`ancestree/assets/`), shared verbatim with the 0.1.x repo.

### 5.4 God-class decomposition (old → new)

| Old (lines) | Becomes |
|---|---|
| `LineageStore` (899) | `store.py` (facade) + `domain/rules.py` + `ingest/packing.py` + `maintenance.py` + `db/metadata_store.py` + `domain/fingerprint.py` |
| `Node` (716) | `domain/node.py` + `ingest/workspace.py` + `domain/metadata.py` + `db/chunk_store.py` + `domain/fingerprint.py` |
| `lineage_database` (286) | `db/{connection,schema,metadata_store}.py` — journal/snapshot/reconcile **deleted** |
| `chunkstore.py` (257) | `db/chunk_store.py` (SQLite, incl. the read cache) + `ingest/cdc.py` |
| `utils.py` (218) | `domain/provenance.py` + `util.py`; matching logic → SQL in `metadata_store` |
| `vis.py` (140) | `web/{graph,export,server}.py` |

### 5.5 Code conventions

- **Frozen dataclasses for value objects** (`NodeRecord`, envelopes, chunk records) — stdlib, self-documenting, and they enforce the immutability AD10 promises.
- **Only `ingest/workspace.py` writes the filesystem.** Everything else goes through SQLite — a greppable guarantee.
- **Server endpoints resolve by database key, never by filesystem path.**
- **The subtle SQL stays confined:** multi-key `find` lives in one well-tested `MetadataStore` method; GC reachability lives in `maintenance.py`.
- **Layer packages, lean root.** The root holds entry points and cross-cutting leaves; everything else lives in `domain/`, `db/`, `ingest/` or `web/`. Resist both God-classes and one-file-per-40-lines fragmentation.

---

## 6. Data Model — SQLite Schema

One database, `ancestree.db`, in WAL mode. Structural and provenance facts are real columns (AD6); the `metadata` table holds only user metadata; chunks, deltas and resemblance features sit alongside.

```sql
PRAGMA user_version = 1;
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
-- Set at creation time (it cannot be enabled later without a full rewrite):
-- lets compact() reclaim space via incremental_vacuum instead of a full VACUUM.
PRAGMA auto_vacuum = INCREMENTAL;

-- Store-wide configuration: rules, gen_triggers, reuse_identical/delta policy.
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                        -- JSON
);

-- One row per node. Structural + provenance facts are real columns.
CREATE TABLE node (
    node_id               TEXT PRIMARY KEY,    -- 8-char id
    step_type             TEXT NOT NULL,
    generation            INTEGER NOT NULL,
    created_utc           TEXT NOT NULL,       -- ISO-8601
    created_epoch_seconds REAL NOT NULL,       -- latest() / colour-by-time
    healthy               INTEGER NOT NULL,    -- 0/1
    duration_seconds      REAL,
    size_bytes            INTEGER NOT NULL DEFAULT 0,
    content_hash          TEXT,                -- content-identity bucket key
    prov_user       TEXT,
    prov_python     TEXT,
    prov_platform   TEXT,
    prov_git_commit TEXT,
    prov_git_branch TEXT,
    prov_git_dirty  INTEGER
);
CREATE INDEX idx_node_step ON node(step_type);
CREATE INDEX idx_node_hash ON node(content_hash);
CREATE INDEX idx_node_gen  ON node(generation);

-- DAG edges: a node may have several parents (a join). Cascades on delete.
CREATE TABLE edge (
    child_id  TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    parent_id TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL,                -- preserves parent order
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX idx_edge_parent ON edge(parent_id);   -- fast children() lookup

-- User metadata only. The envelope is preserved; value is JSON so it holds
-- scalars, lists, dicts, or table blobs. num_value backs numeric ranges.
CREATE TABLE metadata (
    node_id    TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT,                           -- JSON-encoded
    data_type  TEXT NOT NULL,                  -- text|image|link|table|json|code
    grp        TEXT,
    searchable INTEGER NOT NULL,               -- 0/1
    num_value  REAL,                           -- extracted numeric (accuracy>0.9)
    PRIMARY KEY (node_id, key)
);
CREATE INDEX idx_meta_key ON metadata(key)                WHERE searchable = 1;
CREATE INDEX idx_meta_num ON metadata(key, num_value)     WHERE num_value IS NOT NULL;

-- Content-addressed chunk pool. Each chunk stored once (a repeat put is a
-- no-op — that is the exact dedup). A chunk is raw (zlib) or a delta:
-- zlib dictionary-compressed against base_digest. Depth is capped at 1:
-- a base_digest always names a RAW chunk.
CREATE TABLE chunk (
    digest                TEXT PRIMARY KEY,    -- sha256 of the PLAINTEXT chunk
    kind                  INTEGER NOT NULL,    -- 0 = raw(zlib), 1 = delta(zdict)
    base_digest           TEXT REFERENCES chunk(digest),
    data                  BLOB NOT NULL,       -- zlib(raw) or the delta stream
    length                INTEGER NOT NULL,    -- plaintext length
    created_epoch_seconds REAL NOT NULL
);

-- Resemblance index: super-feature -> chunk, for near-duplicate discovery.
CREATE TABLE chunk_feature (
    feature INTEGER NOT NULL,
    digest  TEXT NOT NULL REFERENCES chunk(digest) ON DELETE CASCADE,
    PRIMARY KEY (feature, digest)
);
CREATE INDEX idx_feature ON chunk_feature(feature);

-- One logical output file of a node.
CREATE TABLE artifact (
    node_id TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,                     -- path relative to the node
    size    INTEGER NOT NULL,
    sha256  TEXT NOT NULL,                     -- whole-artifact digest
    PRIMARY KEY (node_id, relpath)
);

-- Ordered recipe: which chunks reconstruct each artifact.
CREATE TABLE artifact_chunk (
    node_id TEXT NOT NULL,
    relpath TEXT NOT NULL,
    ordinal INTEGER NOT NULL,                  -- chunk order within the artifact
    digest  TEXT NOT NULL REFERENCES chunk(digest),
    PRIMARY KEY (node_id, relpath, ordinal),
    FOREIGN KEY (node_id, relpath)
        REFERENCES artifact(node_id, relpath) ON DELETE CASCADE
);
CREATE INDEX idx_ac_digest ON artifact_chunk(digest);   -- reachability / GC
```

How it hangs together: `node` 1─N `metadata`; `node` N─N `node` via `edge` (the DAG); `node` 1─N `artifact` 1─N `artifact_chunk` N─1 `chunk`; a `chunk` may reference another `chunk` (`base_digest`) for delta storage and has N `chunk_feature` rows for resemblance lookup. **Dedup lives at two keys:** `chunk.digest` (identical chunk = same row) and `node.content_hash` (identical node = reused row, the `reuse_identical` policy).

**GC reachability.** A chunk is live if any `artifact_chunk` references it **or** it is the `base_digest` of a live delta chunk — a single hop, because delta depth is capped at 1 (AD5). `compact()` computes this before deleting, so a base can never be reaped out from under its delta.

**Storage notes.** Chunks are ≤256 KB, comfortably inside SQLite's blob limits. `auto_vacuum=INCREMENTAL` is set at creation (it cannot be enabled later without a full rewrite), so `compact()` returns freed pages to the OS without rewriting the whole file; full `VACUUM` stays available as a deep compact.

---

## 7. Runtime Behaviour & Data Flow

### 7.1 Three-tier storage model

| Tier | Location | Role & speed |
|------|----------|--------------|
| **Hot (write buffer)** | `.scratch/<id>/` plain files | Where user code writes. Native OS speed; same-session reads are native. |
| **Durable + queryable** | `ancestree.db` (mmap'd) | Metadata: a small synchronous WAL commit. Chunks: written at ingest. Loaded via memory map, queried via indexes. |
| **Read cache** | `<root>/.cache/<session>/` plain files | First read of a packed artifact reassembles once; every read after is native. Pid-tagged; dead sessions reaped by the store-open sweep. |

SQLite is the durable store and the query engine — it is never on the synchronous hot path for artifact bytes.

### 7.2 Write path

1. `create_node` validates the transition (`RuleEngine`), works out the generation, allocates a node_id, and creates `.scratch/<node_id>/`.
2. User code writes files via `node / "x"` (native) and attaches metadata via `add_meta`.
3. At clean block exit: `packing` reads the scratch files in one pass — chunking, dedup/delta, artifact SHA-256s, total size and the content hash all together.
4. If `reuse_identical` is on and an identical `content_hash` exists (verified against the full content summary), the node is *adopted*: the handle rebinds onto the existing row and the scratch is discarded.
5. Otherwise one transaction writes the `node` row, `edge` rows, `metadata` rows, `chunk`/`chunk_feature` blobs and `artifact`/`artifact_chunk` rows.
6. The scratch directory is deleted.

### 7.3 Read path & artifact resolution

Each node has its **own** scratch directory — there is no shared scratch, so multiple nodes in a session cannot interfere. A node's scratch is deleted once its ingest commits. Reading a logical artifact `(node_id, relpath)` always resolves in the same order:

1. **Loose scratch file** — still there while the node's block is open. Native read.
2. **Read cache** — `<root>/.cache/<session>/<node_id>/<rel>`; reused if already reassembled this session.
3. **Reassemble from SQLite** — fetch the chunks, decode any deltas against their raw bases (depth 1), verify SHA-256, write into the cache, return that path.

So reads of what a node just wrote inside its own block are native; reads of anything already ingested come out of SQLite via the cache. `node / "x"` and `artifacts()` re-resolve every call — don't hold a raw scratch `Path` across the block boundary (it gets deleted at ingest); re-fetch and you always get a valid path.

### 7.4 Query path

`find`/`latest` compile to indexed `WHERE` clauses; `lineage` is a recursive CTE; `children` is an indexed edge lookup. Lambda predicates run in Python over the SQL-narrowed set.

### 7.5 Crash & concurrency semantics

- **Clean block exit** ⇒ node + artifacts committed atomically; durable and visible to other processes straight away.
- **Python exception in the block** ⇒ the partial scratch is ingested with `healthy=False` and stays searchable. Partial work is evidence, not garbage.
- **Hard kill / power loss mid-block** ⇒ the scratch survives with its **seed file**. The next store open sweeps `.scratch/`: any seeded directory whose owning process is dead gets **adopted as an unhealthy node** — so partial work survives even a SIGKILL, which 0.1.x lost. Committed leftovers and unseeded/empty litter get removed; live sessions are never touched.
- **Concurrent processes** ⇒ WAL gives many readers + one writer; the write lock and `busy_timeout` queue writers. Heavy multi-process write concurrency stays a known SQLite limit.

---

## 8. What Gets Deleted — The Simplification Payoff

Gone outright thanks to AD1–AD3 and AD6:

- The disposable-index subsystem: `.index.json`, `.index.log`, reconcile/replay/snapshot/staleness tracking, `rebuild_from_disk`, and the O(N) cold-start directory scan.
- The background-packer state machine: queue, worker, locks, pending-sets, `WeakSet`, `os.register_at_fork`, straggler scans (default configuration).
- All five `*.tmp` + `replace()` dances, and the `.gc.lock` file + 60-second grace window.
- The loose-vs-packed reclaim ordering and the "loose files until the recipe is durable" invariant.
- Per-node directories, `Node.path`, and the directory walk that was copy-pasted five times.
- The system-vs-user key bookkeeping (three key-sets + `_system_keys`).
- `flatten_meta`, `to_db`, and most of `is_match` (→ SQL).
- The read cache's `fcntl` lock files (→ pid-tagged session dirs, reaped by the same sweep that already handles orphaned scratch).
- Three Node constructors + `_hydrate`.
- The doc drift: phantom `compact()` references, the `dedupe`/`chunk` signature-vs-docstring contradiction, the two time parsers.

Net effect: the subtlest ~40% of the old code was deleted rather than moved — and at v2.5 the remaining 0.1.x modules went too.

---

## 9. Trade-offs & Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Single-DB-as-truth** (AD9) | Corrupt `.db` = data loss; no file fallback | WAL, `PRAGMA integrity_check`, backups, on-demand `export()` sidecars |
| **Pure-Python CDC throughput** (~3–10 MB/s Gear loop) | Slow ingest of large artifacts; synchronous block-exit pause | Runs after user code; fixed-boundary fallback keeps huge ingests at C speed (AD4); `background=True` seam; benchmark block-exit time |
| **SQLite on NFS** | Unreliable file locking | Documented non-goal; use local disk; `busy_timeout` for local multi-process |
| **Packed-read latency** | First read of a packed artifact costs reassembly | Loose scratch for recent reads; read cache for repeats; delta depth capped at 1 with a C-speed codec (AD5) |
| **Multi-process write concurrency** | SQLite serialises writers | WAL + `busy_timeout`; fine for the single-writer common case |
| **Backward incompatibility** | 0.1.x stores won't open | Accepted (v2.5): a clean break; 0.1.x stays installable from PyPI for old stores |
| **Rewrite risk** (rebuild + API break + test rewrite land together) | Regressions during the transition | The rewritten suite (unit + integration + property tests over every subsystem) is the safety net; legacy code was only deleted once it was fully covered |

---

## 10. Implementation Roadmap

Each phase is independently testable and leaves the suite green. I tick items as they merge.

**Safety-net policy (retired at v2.5):** originally the 0.1.x modules and their tests stayed green until Phase 9. I then dropped backwards compatibility entirely, so the legacy modules, their tests and the old assets were deleted right after Phase 7 — the rewritten suite is the safety net now.

### Phase 0 — Scaffolding
- [x] Create the new package skeleton (empty modules per [§5.1](#51-package-layout)) alongside the current code.
- [x] Add this blueprint to the repo; set up a `rebuild` tracking branch.
- [x] Confirm CI (ruff, mypy `strict`, pytest) green on unchanged code.
- **Exit:** skeleton imports; CI green.

### Phase 1 — Persistence foundation
- [x] `db/connection.py` — pragmas, per-thread/PID connections, write lock.
- [x] `db/schema.py` — DDL from [§6](#6-data-model--sqlite-schema), `ensure_schema`.
- [x] `errors.py` — pulled forward from Phase 2 (`SchemaError` backs schema verification).
- **Exit:** create/open a `.db`, schema verified, connection & fork-safety unit tests pass.

### Phase 2 — Metadata store & queries
- [x] `util.py`, `domain/provenance.py` (`errors.py` landed with Phase 1).
- [x] `db/metadata_store.py` — `add_node`, `find`, `lineage` (recursive CTE), `children`, `most_recent`, `find_by_hash`, `remove`.
- **Exit:** the query/lineage semantics ported and passing against SQLite.

### Phase 3 — CDC Layer 1, chunk store & sync packing
- [x] `ingest/cdc.py` chunker section (FastCDC lifted from `chunkstore.py`) + fixed-boundary large-file fallback (AD4).
- [x] `db/chunk_store.py` (SQLite BLOBs, exact dedup, reassembly, session read cache in system temp), `ingest/workspace.py` (incl. the scratch seed file).
- [x] `ingest/packing.py` — synchronous ingest at block-exit.
- **Exit:** exact-dedup chunking ports pass; artifacts round-trip through SQLite; round-trip property tests (random data & mutations) green.

### Phase 4 — Domain decomposition & facade
- [x] `domain/{metadata,rules,node}.py`, `store.py`.
- [x] Wire `create_node`, context-manager, `add_meta`, `artifacts`, `__truediv__`.
- [x] `store.sql()` read-only escape hatch (`query_only` connection) and `store.stats()` (counts, sizes, dedup ratio).
- **Exit:** the suite rewritten to the redesigned API ([§11](#11-public-api)) is green against the new backend.

### Phase 5 — Identical-node reuse & maintenance
- [x] `domain/fingerprint.py`; `content_hash` column; adopt/rebind on identical content.
- [x] `maintenance.py` — `Pruner` (SQL cascade prune), `compact()` (orphan-chunk GC + `incremental_vacuum`), orphan-scratch sweep at store open.
- **Exit:** reuse_identical semantics fully green on the new API (finishes what my original feature branch started); prune/compact/sweep tests pass.

### Phase 6 — CDC Layer 2 (advanced dedup)
- [x] `ingest/cdc.py` resemblance + delta sections (super-features computed in the chunking pass; zlib-`zdict` codec); wire into `packing`/`ChunkStore` behind the `chunk` policy.
- [x] Enforce delta depth 1 (bases are raw); extend `compact()` reachability to live bases; round-trip property tests through the delta path.
- [x] **Benchmark gate:** measure dedup ratio and ingest/read overhead on near-duplicate fixtures; set the Layer-2 default from the data (AD5). → **2.3× less storage for 1.07× ingest; default ON** (`docs/benchmarks/RESULTS.md`). *(Re-measured at the shipping 16 KiB chunk size: 2.23× on that synthetic random-byte fixture, 3.93× on the mixed corpus — see v2.8 and v3.2 below.)*
- **Exit:** benchmark results recorded in the repo; delta/resemblance tests green; the adversarial suite green.

### Phase 7 — Web static export
- [x] `web/graph.py`, `web/export.py` — structured templating + artifact materialization.
- **Exit:** export coverage rewritten and green; the generated HTML opens offline with working artifact links.

### Phase 8 — Local live server
- [x] `web/server.py` — `serve_graph()`; SQL-backed endpoints (`/api/graph`, `/api/node`, `/api/search`, `/api/diff`, `/api/runs`, `/api/artifact`); on-demand artifact reassembly.
- [x] Live front end — thin client fetching from the API; no client-side query engine (AD11).
- [x] **Explorer parity (gap review):** the 0.1.x file's README-marketed search, node **diff** and sortable **runs table** live HERE now — the static export stays view-only, so the live server is where that parity had to land.
- [x] `__main__.py` CLI with the `serve` subcommand (`python -m ancestree serve <root>`).
- **Exit:** server serves the skeleton and streams search/diff/detail via SQL on `127.0.0.1`; runs-table and diff render from API data; smoke tests pass.

### Phase 9 — Release polish
- [x] Complete the CLI: `export` and `compact` subcommands (`serve` landed with Phase 8).
- [x] Update `README.md`/docs — SQLite, the NFS caveat, the live server, and the marketing claims ("no database" becomes "no database *to run*"; the static export is a view-only snapshot); park the old-API example notebooks as legacy records. *(The mkdocs pages get their refresh in the `rebuild` → `main` PR.)*
- [x] Version bump to **0.2.0** + CHANGELOG with the old→new API cheat-sheet.
- **Exit:** CLI complete; docs updated; CI green.

*(The deferred legacy-code deletion left this phase at v2.5: no backwards compatibility means the 0.1.x modules, tests and assets went right after Phase 7.)*

---

## 11. Public API

Backwards compatibility is **not** a goal (pre-1.0). The API is redesigned around one coherent vocabulary; the only fixed points are the write/read feel (HC2) and zero dependencies (HC1). See [AD10](#ad10--redesign-the-public-api-for-coherence).

### Redesigned surface

**Creation & recording** — `store.create_node(step_type, parent=None)` yields a mutable recording handle: `handle / "file"` (write target) and `handle.add_meta(...)`. Unchanged in feel (HC2).

**Queries** (return the immutable `Node` record, or a list of them):

| New | Was | Notes |
|-----|-----|-------|
| `store.get(id)` | `get_node` | resolve one node |
| `store.find(**filters)` | `find_node` | equality/range in SQL; lambdas in Python; `parent_id=[…]` matches parents |
| `store.latest(**filters)` | `get_most_recent_node` | most recent match |
| `store.lineage(node)` | `get_lineage` | ancestry, oldest first |
| `store.children(node)` | `get_child_nodes` | direct children |
| `store.ancestors(node, **filters)` | `find_in_lineage` | filtered ancestry |
| `store.from_parent(node, pattern)` | `from_parent` | unchanged |

**Node record** — `node.node_id`, `.step_type`, `.generation`, `.parent_id`, `.created_utc`, `.healthy`, `.duration_seconds`, `.size_bytes`, `.metadata`, `.provenance`, `.artifacts(pattern)`, `node / "file"` (read → readable path). Immutable and hashable. `Node.path` is **gone**.

**Maintenance & output** — `store.prune(node, dry_run=True)`, `store.compact()` (replaces `gc`/`flush`/`clear_cache`), `store.export_metadata(dest=None)` (grep-able `meta.json` sidecars), `store.export_graph()` (view-only static HTML), `store.serve_graph(port=0)` (the searchable explorer).

**Power queries & introspection** — `store.sql(query, params=())`: any read-only `SELECT` over the documented schema, on a `query_only` connection (the schema is a versioned public contract via `PRAGMA user_version` and `schema.py`); the natural first step toward DataFrame querying. `store.stats()`: node/chunk counts, artifact vs stored bytes, dedup ratio, database size — makes the deduplication visible.

**CLI** — `python -m ancestree serve|export|compact <root>` (stdlib `argparse`).

**Store policy** — `LineageStore(root, rules=None, gen_triggers=None, reuse_identical=..., delta=...)`; `reuse_identical`/`delta` are persisted at creation like `rules`, not re-passed per open.

**Removed** — `Node.path`, `rebuild_db_from_disk()`, `gc()`, `flush()`, `clear_cache()` (folded into `compact()` / automatic lifecycle).

### Coming from 0.1.x

A deliberate **clean break** (v2.5): the on-disk format and the API both change, and 0.1.x stores are not readable by the rebuild — anyone needing an old store keeps 0.1.x installed for it. The rebuild ships as **0.2.0** (pre-1.0, so a breaking minor release is legitimate); the renames above are the whole API delta and are mechanical.

---

## 12. Future Work

- **DataFrame SQL querying.** Store pandas DataFrames so SQL can query into their contents (node-discovery-by-DataFrame-facts, full relational materialisation, or an on-demand `store.sql(...)` over stored tables). The schema's JSON `value` column and `data_type='table'` leave room; pandas stays optional.
- **Background CDC hardening.** Promote the `background=True` seam to a first-class worker if large-artifact profiles ever demand it.
- **Streaming huge artifacts.** `sqlite3.Connection.blobopen()` (Python 3.11+) for incremental chunk I/O without whole-chunk buffers, once 3.9/3.10 support is dropped.

---

## 13. Glossary

- **Node** — one step in a pipeline; a row in the `node` table with metadata, edges and artifacts.
- **Artifact** — an output file a node produced; stored as an ordered list of chunks.
- **CDC (Content-Defined Chunking)** — splitting data at boundaries chosen by the content itself (via a rolling hash), so edits shift only local chunks and near-identical files share most chunks.
- **Chunk** — a variable-length, content-addressed (SHA-256) unit of artifact bytes; stored once.
- **Delta chunk** — a chunk stored as zlib dictionary-compression against a similar *raw* base chunk (Layer 2; depth capped at 1).
- **Super-feature** — a cheap min-wise hash summarising a chunk, used to find similar chunks.
- **Scratch** — the transient `.scratch/<node_id>/` directory a node's artifacts get written into before ingest.
- **Read cache** — the ephemeral, session-scoped copies of reassembled packed artifacts, kept at `<root>/.cache/<session>/` (dead sessions reaped by the store-open sweep).
- **Lineage** — the transitive ancestry of a node (a recursive walk over `edge`).

---

## Decision Log

| Date | Change |
|------|--------|
| 2026-07-07 | v1.0 — first version of the blueprint. Core calls: SQLite as the single source of truth (metadata + chunks); nodes as rows not folders; SQLite-owned atomicity; synchronous CDC by default with a `background` seam; two-layer dedup; structural/provenance as columns; SQL search; keep the `/` ergonomics and the static export, add a local server; accept the single-DB resilience posture. DataFrame SQL querying parked. |
| 2026-07-07 | v1.1 — dropped public-API backwards compatibility as a constraint (pre-1.0). Added AD10 (API redesign); reframed HC3; rewrote §11 as a redesigned surface. The old tests get rewritten to the new API rather than pinned. |
| 2026-07-07 | v1.2 — added AD11: the live server answers search/diff/detail with server-side SQL and the browser is a thin client — no client-side grep. One query grammar (Python → SQL) shared by `store.find` and `/api/search`. Left the static-export search question open. |
| 2026-07-07 | v1.3 — static export decided: **view-only snapshot**. No query box in the file, the JS query engine goes entirely, and exactly one query implementation exists. Also pinned down §7.3 artifact resolution (per-node scratch; loose → cache → reassemble). |
| 2026-07-07 | v2.0 — review pass over the whole plan. **AD5 reworked:** swapped my planned pure-Python copy/insert delta codec (which would have been the slowest code in the build) for **zlib `zdict` dictionary compression** — C speed, ~20 lines — with **delta depth capped at 1**, and made Layer 2's default subject to a Phase 6 benchmark instead of assuming it. **AD4 honesty pass:** corrected the Gear-loop throughput estimate to ~3–10 MB/s and added the fixed-boundary large-file fallback. **Crash forensics promoted** into the plan: a scratch seed file + a startup sweep that adopts hard-killed partial work as unhealthy nodes (better than 0.1.x, which lost it). Schema gets `auto_vacuum=INCREMENTAL` at creation. Maintenance moved from Phase 9 to Phase 5. Kept deliberately: chunks in SQLite, synchronous CDC, lambda predicates, the view-only export. |
| 2026-07-07 | v2.1 — soundness refinements. **Read cache relocated** to the system temp dir (OS-cleaned; the store root at rest is just the database). **Structure trimmed:** `readcache.py` folded into `db/chunk_store.py`; the `cdc/` package collapsed into one sectioned `cdc.py`. **Added:** `store.sql()` (read-only escape hatch — the schema becomes a versioned public contract), `store.stats()`, a stdlib CLI, and the §5.5 code conventions. Multi-key `find` confined to one method; WAL checkpointed after large ingests; explicit safety-net policy while the rewrite was in flight. |
| 2026-07-07 | v2.2 — layered package layout, off the back of Phase 0 feedback that the flat root had far too many modules. Root keeps entry points and cross-cutting leaves; everything else moved into `domain/`, `db/`, `ingest/`, `web/`. |
| 2026-07-08 | v2.3 — **Layer-2 benchmark done (AD5): `chunk` defaults ON.** On the target workload (12 near-duplicate versions, ~1% scattered in-place edits) Layer 2 stored **2.3× less** than Layer 1 alone (ratio 2.31 vs 1.00) for 1.07× ingest and sub-millisecond read overhead — `docs/benchmarks/RESULTS.md`. Two implementation notes worth remembering: resemblance uses min-wise transforms over strided 8-byte samples (a wrong candidate only costs one trial encode, because a delta is kept solely when it beats plain compression); and the zdict has to be truncated to 32,256 bytes because zlib's usable match distance is `w_size − MIN_LOOKAHEAD` (32,506) — a full-32K dictionary leaves aligned content exactly one step out of reach and every delta silently degenerates. Found that one the hard way. |
| 2026-07-08 | v2.4 — mid-rebuild parity review against the 0.1.x surface (the goal is an SQL transition that keeps every functionality). Fixed in code: **`find(parent_id=…)` restored** (parents live in the edge table now, so it silently matched nothing) and **`store.export()` delivered** (promised in §11 but scheduled in no phase). Fixed in the plan: the old explorer's **runs table and node diff** pinned as explicit Phase 8 exit criteria. Confirmed deliberate, not gaps: `Node.path`, `rebuild_db_from_disk`, `gc`/`flush`/`clear_cache` → `compact`, structural facts as attributes. Added `docs/examples/sql_backend_quickstart.ipynb`, executed end to end. |
| 2026-07-08 | v2.5 — **dropped backwards compatibility entirely.** The 0.1.x modules, their tests and the old assets are deleted now rather than at Phase 9, and the package exports flip to the new API. Old stores stay on 0.1.x. Compat shims removed from the new code (`InvalidTransition` no longer subclasses `ValueError`; the alias reserved keys go). The safety-net policy is retired — the rewritten suite is the safety net. Phase 9 shrinks to release polish. |
| 2026-07-09 | v2.6 — **read cache moved back inside the store**: `<root>/.cache/<pid>-<suffix>/` instead of the system temp dir, so the paths reads hand back make sense at a glance and the layout matches what I'm used to from 0.1.x. This partially reverses v2.1 — the reason it lived in temp was "zero reaping code", and that argument expired when the Phase 5 sweep landed: it already runs at every store open with a pid-liveness check, so reaping dead cache sessions is ~15 extra lines on machinery that exists anyway. Cache dirs hold only derived data, so reaping can never lose work. Reads deliberately do NOT resolve into `.scratch` (the other half of the question that prompted this): the sweep adopts orphaned scratch as unhealthy nodes, so cache copies there would masquerade as crashed work. |
| 2026-07-09 | v2.7 — **the classic explorer is back as live mode** (my call): the server renders the 0.1.x `template_new.html` + `styles.css` + `actions.js` with the whole store as `window.PIPELINE_DATA`, fresh on every page load — I missed the old front end, and a refresh is all the "live" a local tool needs. Its search/diff/runs table run client-side; the SQL JSON API stays for programmatic use, so AD11 is amended rather than reversed (the Python→SQL grammar remains the one the library exposes). `live.html` deleted; `ancestree/assets/` is tracked and ships again. `host_live_graph` is now non-blocking by default, opens the browser, and re-running it replaces the previous server; store teardown runs via a `weakref` finalizer so close() is optional. Repo cleanup in the same pass: the temporarily-restored V1 modules and their test suite deleted again, and the docs site pages (index, caveats, reference, examples, mkdocs nav) rewritten for the new API — no V1 references remain outside `docs/examples/legacy-0.1/`, which stays on record. |
| 2026-07-25 | v2.8 — **chunk encoding and indexes settled before release.** The average chunk size drops to 16 KiB so a delta base fits inside zlib's 32,256-byte dictionary window (−14% stored, −22% ingest), payloads zlib cannot shrink are stored verbatim (kind 2), and `idx_meta_key` covers `(key, value)`. |
| 2026-07-26 | v3.0 — **the public vocabulary made consistent before release.** `dedup` → `reuse_identical` and `chunk` → `delta` (the old `chunk=False` never disabled chunking — Layer 1 always runs — so the name described the wrong thing); `NodeRecord.parent_ids` → `parent_id`, matching the record, the filter and the edge column; and the three output methods take one verb pair each: `generate_web_graph` → `export_graph`, `host_live_graph` → `serve_graph`, `export` → `export_metadata`, which also lines the API up with the `serve`/`export` CLI. All pre-1.0 and pre-release, so no deprecation shims. |
| 2026-07-26 | v2.9 — **schema version stamping removed.** `SCHEMA_VERSION`, `schema_version()` and the version checks in `ensure_schema` are deleted; a store no longer records what wrote it. `ensure_schema` now creates the schema or verifies the expected tables are present, which still refuses a SQLite file ancestree did not write. The one fact worth stating is that 0.1.x stores do not open in 0.2.0, and it is stated once, in the caveats. |
| 2026-07-26 | v3.2 — **the benchmarks became documentation, and the headline number moved onto a reproducible measurement.** `benchmarks/` moved to `docs/benchmarks/` so the two notebooks render as site pages instead of sitting outside `docs_dir` where mkdocs cannot reach them, and `RESULTS.md` — referenced from five places and never actually written — now exists there, tabulating every recorded output of both notebooks. Writing it exposed the problem: the published **2.63×** was measured on a corpus of real files that scatters its insertions and deletions, and that corpus is in no commit. The notebook corpus of the same description measures **3.93×** on the pool and **3.13×** on the whole file. Rather than keep quoting an unverifiable number because it flattered less, the claim in the README, the CHANGELOG and here now points at the measurement anyone can re-run. The lesson worth keeping: a headline that cannot be regenerated from the repository is not a result, it is a memory. |
| 2026-07-26 | v3.1 — **v2.9 reversed: version stamping restored, and the store root guarded.** Reverting it was the wrong call to make in the week the on-disk format goes public, because it cannot be retrofitted onto stores already written. Table-name presence is not a substitute: the chunk encoding, or the meaning of a column, can change without the schema's shape changing at all, so a 0.3.0 would open a 0.2.0 store and misread it rather than refuse it. `SCHEMA_VERSION`, `schema_version()` and the three refusal branches (unstamped / newer / older) are back, and `store.sql()`'s "versioned public contract" (v2.1) means something again. Separately, `_refuse_legacy_root` closes the hole a stamp could never cover: a 0.1.x root has no database to stamp, so `ensure_schema` took the fresh-creation path and handed back an **empty store sitting beside the user's old node directories** — the work was still on disk, but it read as gone. A root holding `.lineage_config.json` and no `ancestree.db` is now refused before anything is created. |
