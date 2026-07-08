# Ancestree Rebuild Blueprint & Action Plan

> **Version:** 2.2 &nbsp;•&nbsp; **Date:** 2026-07-07 &nbsp;•&nbsp; **Author:** Joshua Smith &nbsp;•&nbsp; **Status:** Approved for implementation

A living design document for consolidating Ancestree onto a single SQLite backing store, adding advanced content-defined deduplication, decomposing the two large classes, and adding an optional local interactive server — **without adding a single third-party dependency.**

**How to use this document.** It is both a *report* (why we are doing this) and an *action plan* (how, in order). The [Implementation Roadmap](#10-implementation-roadmap) uses task lists — tick them off as phases land. When a decision changes, update the relevant entry in [§4](#4-key-architectural-decisions) and add a line to the [Decision Log](#decision-log). Treat this file as the single source of truth for the rebuild.

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
11. [Public API and Migration](#11-public-api-and-migration)
12. [Future Work](#12-future-work)
13. [Glossary](#13-glossary)
14. [Decision Log](#decision-log)

---

## 1. Executive Summary

**Ancestree** is a zero-dependency, pure-stdlib data-lineage tracker. Every step of a pipeline is a *node*; nodes chain into a directed acyclic graph (DAG) that is durable on disk, queryable in plain Python, and renderable as a single self-contained interactive HTML file.

Today the store is a tree of directories: one folder per node, each holding a `meta.json`, an artifact manifest, and artifact files, backed by a hand-rolled JSON index (snapshot + append-only journal + directory reconcile) and an on-disk content-addressed chunk pool.

This rebuild **consolidates all durable state into a single SQLite database (`ancestree.db`)** — metadata *and* deduplicated chunks — while keeping the package dependency-free (`sqlite3` and `http.server` are standard library). In doing so it:

- **Deletes the subtlest ~40% of the codebase** — the snapshot/journal/reconcile index, the filesystem crash-ordering scaffolding (temp-file rename dances, loose-vs-packed reclaim, straggler scans, GC lock files), and the per-node directory machinery — by letting SQLite transactions own atomicity and durability.
- **Makes nodes rows, not folders.** A node is a database row; a short-lived scratch directory exists only while its artifacts are being written, then is ingested and deleted.
- **Upgrades deduplication** from exact-chunk matching to a two-layer scheme: FastCDC (variable, content-defined chunking) plus resemblance-matched **delta storage via zlib dictionary compression** — C-speed, stdlib-only — that captures near-duplicate blocks exact chunking cannot.
- **Decomposes the two large classes** (`LineageStore`, 899 lines; `Node`, 716 lines) into focused, testable units.
- **Adds an optional local interactive server** (`host_live_graph()`, stdlib `http.server`) alongside — not replacing — the emailable single-file HTML export.

The result is faster loading (memory-mapped, indexed queries instead of an O(N) directory reconcile on every cold start), native-speed writes (the user's code path is unchanged), dramatically simpler persistence, and higher storage efficiency — with **no new dependencies** and a **cleaner, deliberately redesigned public API** (backward compatibility with 0.1.x is explicitly not a goal — see [§11](#11-public-api-and-migration)).

This is a **consolidation onto a better substrate**, not a rescue of bad code. Good existing logic will be preserved, not rewritten.

---

## 2. Goals, Non-Goals & Hard Constraints

### Goals

| # | Goal | Why |
|---|------|-----|
| G1 | Single SQLite backing store for metadata **and** chunks | Advanced querying, complex datatypes, one portable file, atomic durability |
| G2 | Advanced deduplication (variable CDC + near-duplicate delta) | Maximise storage efficiency; "find repeated blocks" beyond exact matches |
| G3 | Decompose `LineageStore` and `Node` into focused classes | Maintainability; the two God-classes are hard to reason about |
| G4 | Optional local interactive server | Always-current graph during a run, without a rebuild step |
| G5 | Simpler, native-speed persistence & loading | Remove "loose logic trailing around"; fast cold start |
| G6 | A coherent, deliberately-designed public API | The point of the rebuild; compatibility with 0.1.x is not required |

### Non-Goals (this phase)

- **DataFrame SQL querying** — storing pandas DataFrames as first-class relational tables and querying into their contents. Deferred to [Future Work](#12-future-work); the schema leaves room for it.
- **Networked / multi-user server** — the live server binds to `127.0.0.1` only. No auth, no remote hosting.
- **NFS safety** — SQLite file-locking over NFS is unreliable; the previous file-based guarantee is intentionally relaxed (see [§9](#9-trade-offs--risks)).

### Hard Constraints (non-negotiable)

- **HC1 — Zero dependencies.** `pip install ancestree-track` pulls nothing but the standard library. This forbids Postgres/MySQL, Flask/FastAPI, zstd/bsdiff/xdelta, and pandas-as-a-requirement. `sqlite3`, `http.server`, `zlib`, `hashlib` are all stdlib and permitted.
- **HC2 — Preserve the read/write ergonomics.** `node / "file.csv"` returns a real path to write to; `node.artifacts()` returns readable paths. Users must not have to learn new syntax.
- **HC3 — Ergonomics over compatibility.** Backward compatibility is explicitly *not* required (the package is pre-1.0). The public API is free to change wherever that improves the design; the only fixed points are the *feel* of writing/reading artifacts (HC2) and the zero-dependency rule (HC1). The redesign and the 0.1.x migration are specified in [§11](#11-public-api-and-migration) and [AD10](#ad10--redesign-the-public-api-for-coherence).

---

## 3. Current-State Assessment & Redundancy Audit

### 3.1 Modules today

| Module | Lines | Responsibility |
|--------|-------|----------------|
| `core.py` | 899 | `LineageStore` — config, node creation, rules, dedup, background packer, querying, prune, GC, viz |
| `models.py` | 716 | `Node` — metadata envelopes, content hashing, chunk packing/reassembly, manifest, path resolution |
| `database.py` | 286 | `lineage_database` — JSON index: snapshot + append-only journal + directory reconcile |
| `chunkstore.py` | 257 | FastCDC chunker, content-addressed chunk pool, session read cache |
| `utils.py` | 218 | Metadata matching, JSON coercion, provenance capture, time parsing |
| `vis.py` | 140 | Build graph payload; inline assets into one HTML file |
| `assets/` | ~2200 | `actions.js`, `styles.css`, `template_new.html`, `vis-network.min.js` |

### 3.2 Redundancies, anti-patterns & "loose logic"

The dominant issue is **not** the storage medium — it is the *filesystem crash-ordering scaffolding* required because a filesystem has no transactions. Concretely:

- **Hand-rolled atomicity, five times.** `meta.json`, `.artifacts.json`, chunk files, `.index.json`, and the config file each implement their own `*.tmp` + atomic-`replace()` dance with unique temp names to survive concurrent writers.
- **Deferred-packing state sprawl.** `LineageStore` carries seven pieces of worker state (`_pack_queue`, `_pack_worker`, `_pack_lock`, `_enqueued`, `_reclaim_pending`, `_flush_registered`, `_scan_done`) plus a module-level `_live_stores` `WeakSet` and an `os.register_at_fork` handler, all to keep a background chunker crash- and fork-safe.
- **Loose-vs-packed reclaim ordering.** Artifacts exist as loose files "until their recipe is durable", then are reclaimed at a quiescent point — plus a straggler scan (`_scan_stragglers`) to recover from crashes.
- **A disposable index that must reconcile against disk.** `_reconcile`, `_replay_log`, `_write_snapshot`, `_is_stale`, `_refresh_if_stale`, `rebuild_from_disk` — and an O(N) `iterdir()` on every cold start.
- **Directory walking duplicated across five methods.** The `rglob("*")` + skip-`meta.json`/`.artifacts.json`/`.tmp` filter appears in `_artifact_digests`, `_artifact_rels`, `_pack`, `_reclaim_loose`, and `_scan_stragglers_inner`.
- **System-vs-user distinction maintained by convention.** Three overlapping key-sets (`_RESERVED_KEYS`, `_PROVENANCE_KEYS`, `_NON_CONTENT_KEYS`) plus a `_system_keys` snapshot keep structural, provenance, and user metadata apart inside one flat dict.
- **Read-cache reaping machinery.** `fcntl` per-session lock files and a `_reap_dead_sessions` scan to clean reassembled temp files after crashed sessions.
- **Three Node constructors + lazy hydrate.** `_create` / `_load` / `_from_index` + `_hydrate`.
- **Naming & documentation drift.** `lineage_database` is lowercase and is a JSON index, not a database; `create_node`'s signature says `dedupe=True, chunk=True` while its docstring says both default to `False`; docstrings reference a `compact()` method that does not exist (the real method is `gc()`); `parse_time` vs `parse_iso_utc` are two time parsers.
- **Fragile web templating.** `vis.py` inlines assets by replacing exact strings such as `<script src="../../web_app/vis-network.min.js"></script>`.

### 3.3 What is already good (keep, don't rewrite)

- **FastCDC** (`chunkstore.py`) — a correct normalized-chunking implementation with a fixed Gear table.
- **Node content fingerprinting** (`content_hash`, `_content_equal`) — the node-level dedup feature currently being finished.
- **Provenance capture** (`utils.py`) — user / Python / platform / git commit / branch / dirty flag.
- **Crash-forensic semantics** — partial output from a failed step is kept and flagged `healthy=False`.
- **The visual explorer** (`assets/`) — lineage graph, search, diff, dark mode, runs table.

---

## 4. Key Architectural Decisions

Each decision records its rationale, its trade-off, and its status. These are the load-bearing choices; everything in [§5](#5-target-architecture--directory-layout)–[§7](#7-runtime-behaviour--data-flow) follows from them.

### AD1 — SQLite is the single source of truth (metadata **and** chunks)
- **Decision.** All durable state lives in one `ancestree.db` (WAL mode). The JSON index, config file, per-node `meta.json`/manifest, and the `.chunks/` pool are replaced by tables.
- **Rationale.** Advanced querying, complex datatypes, indexed lookups, and — critically — *transactional atomicity and durability for free*, which deletes the entire hand-rolled crash-ordering layer. `sqlite3` is stdlib, so HC1 holds. Keeping chunks and metadata in **one transactional domain** is itself load-bearing: a half-migration (metadata in SQL, chunks on the filesystem) would reintroduce exactly the two-phase crash-ordering problem this decision deletes.
- **Trade-off.** Removes the "files are the database / grep-able / rebuild-from-disk" safety net. See [AD9](#ad9--accept-single-db-as-truth-resilience-posture).

### AD2 — Nodes are rows, not folders
- **Decision.** A node is a `node` row. A transient `.scratch/<node_id>/` directory exists only during a `create_node` block, is ingested into SQLite on block-exit, and is deleted.
- **Rationale.** Once SQLite owns metadata and chunks, the durable per-node directory is vestigial. This deletes `Node.path`, the five `rglob` walks, the loose-vs-packed resolution, and turns `prune` into a SQL cascade.
- **Trade-off.** A scratch workspace is still required because external writers (pandas, PIL, `np.save`) need a real filesystem path (HC2). "No folders" therefore means *no durable folders*.

### AD3 — SQLite transactions own atomicity
- **Decision.** Every write is a transaction. Delete all `*.tmp`+`replace()` dances, the `.gc.lock`, the loose-file reclaim ordering, and the manifest swap.
- **Rationale.** This is where most "loose logic" lives; it is scaffolding for the absence of transactions, which SQLite supplies.
- **Trade-off.** None material. Chunk-ingest becomes one atomic unit (BLOBs + artifact rows commit together, or not at all).

### AD4 — Synchronous CDC by default; background is an opt-in flag
- **Decision.** Chunking runs synchronously at block-exit (after the user's code). A `background=True` policy exists as a seam but is off by default.
- **Rationale.** The user's write path is already native-speed (files to scratch); chunking happens after their code at the `with`-exit. Synchronous deletes the worker thread, queue, locks, fork handler, and straggler scan, and guarantees "closed == saved & visible to other processes."
- **Trade-off.** Block-exit blocks for the chunking time. The per-byte Gear loop is pure Python — realistically **~3–10 MB/s** (SHA-256 and zlib are C-speed and are not the bottleneck) — so block-exit is imperceptible for KB–MB nodes but grows to tens of seconds for 100 MB+ artifacts. Two mitigations: **(1) large-artifact fallback** — files above a size threshold (default ~64 MB) skip the Gear loop and are chunked at fixed max-size boundaries, keeping ingest at C speed (whole-file and repeated-content dedup is kept via chunk hashing; only insert-shift resilience is sacrificed, which matters least for huge binaries); **(2)** the `background=True` seam. **Choose background only when all three hold:** (a) nodes routinely write 100 MB+ *and* the fallback's dedup trade is unacceptable, (b) the per-block pause is observed (interactive/latency-sensitive), and (c) there is GIL-releasing work (numpy/pandas/`subprocess`/IO) between writes for the worker to overlap with. Background is a *latency-hiding* tool, not a *speed* tool — total work is identical.

### AD5 — Two-layer deduplication
- **Decision.** *Layer 1:* FastCDC → eliminate byte-identical chunks (`INSERT OR IGNORE`). *Layer 2:* derive min-wise "super-features" in the same pass as chunking, look up *similar-but-not-identical* stored chunks, and store the newcomer as a **delta**: `zlib` dictionary compression with the base chunk as the preset dictionary (`zlib.compressobj(zdict=base)`), kept only when smaller than the raw encoding. **Delta depth is capped at 1 — a base is always a raw chunk** — so any read costs at most two chunk fetches and GC reachability is a single hop.
- **Rationale.** Captures near-duplicate blocks that exact CDC structurally misses — the real "find repeated blocks" win. Using DEFLATE's dictionary mechanism instead of a hand-written copy/insert codec keeps the codec **C-speed and ~20 lines** (encode = compress with `zdict`; decode = decompress with `zdict`), eliminating what would otherwise be the slowest pure-Python component in the build. Stdlib-only (HC1).
- **Limits & gate.** DEFLATE's 32 KB window means a base contributes at most ~32 KB of reference data — full coverage at the 32 KB average chunk size, partial for larger chunks. Layer 2 ships behind the `chunk` policy with its **default decided by a Phase 6 benchmark gate** (measured dedup ratio vs ingest/read overhead on realistic near-duplicate fixtures); if the win is marginal it defaults off.
- **Trade-off.** Reading a delta chunk fetches its base too (bounded by depth-1); the resemblance index adds one lookup plus a few rows per new chunk; `compact()` must keep live bases (a one-hop closure).

### AD6 — Structural & provenance fields become columns
- **Decision.** `step_type`, `generation`, `timestamp`, `healthy`, `duration_s`, `size`, `content_hash`, and provenance are columns on `node`. The `metadata` table holds **only** user metadata.
- **Rationale.** The system-vs-user distinction becomes *schema*, enforced by the database, deleting `_system_keys` and the three reserved-key sets.
- **Trade-off.** None material.

### AD7 — Search is SQL; predicates fall back to Python
- **Decision.** `find_node` / `find_in_lineage` compile to `WHERE` clauses (equality + numeric ranges via a `num_value` column). Lineage is a recursive CTE; children are an indexed edge lookup. Callable predicates (lambdas) run in Python over the SQL-narrowed candidate set.
- **Rationale.** Collapses `flatten_meta`, `to_db`, and most of `is_match`; scales past linear scans.

### AD8 — Keep the `/` ergonomics and the static export; add the server
- **Decision.** `node / "x"` and `node.artifacts()` keep their feel (HC2). `generate_web_graph()` still emits an emailable single-file HTML, now a **view-only snapshot** (graph + click-to-view metadata, no search box — see AD11). `host_live_graph()` is **added** as the searchable, always-current explorer, not a replacement for the snapshot.
- **Rationale.** Preserve a shareable offline artifact while moving rich exploration and search to the server.

### AD9 — Accept single-DB-as-truth resilience posture
- **Decision.** A corrupt `ancestree.db` is real data loss with no filesystem fallback. Recovery story: WAL + `PRAGMA integrity_check` + periodic backup + on-demand `export()` to grep-able `meta.json` sidecars.
- **Rationale.** The cost of the simplicity in AD1–AD3. Stated explicitly so it is a conscious choice, not a surprise.

### AD10 — Redesign the public API for coherence
- **Decision.** Backward compatibility is dropped as a constraint (pre-1.0). The public API is redesigned for a consistent vocabulary and to shed surface that is meaningless after the rebuild. 0.1.x users get a documented migration.
- **Removed.** `Node.path` (nodes are rows, not directories); `rebuild_db_from_disk()` (there is no separate index to rebuild); `flush()`/`clear_cache()` (lifecycle is automatic; space is reclaimed by `compact()`).
- **Renamed** into one query vocabulary: `get_node`→`get`, `find_node`→`find`, `get_most_recent_node`→`latest`, `get_child_nodes`→`children`, `get_lineage`→`lineage`, `find_in_lineage`→`ancestors`. (Names are adjustable to taste.)
- **Reshaped.** `dedupe`/`chunk` become persisted store policy set once at creation (like `rules`), not per-call flags; maintenance converges on `compact()`; visualisation on `generate_web_graph()` (static) + `host_live_graph()` (live).
- **Restructured (recommended).** Split the mutable **recording handle** yielded by `create_node` (write API: `/`, `add_meta`) from the immutable **`Node` record** returned by queries (read API: attributes, `metadata`, `artifacts`). This removes the current footgun of calling `add_meta` on a queried node and lets the record be an immutable, hashable value object.
- **Rationale.** The current verbs mix `get_*`/`find_*` inconsistently and several methods are meaningless post-rebuild; a coherent API is a primary goal of the rebuild, not a casualty of it.
- **Trade-off.** A breaking change for 0.1.x (acceptable pre-1.0). The existing test suite is rewritten to the new API rather than pinned to the old one; HC2 still fixes the *feel* of `node / "x"` and reading artifacts back.

### AD11 — The live server queries the database directly; no client-side "grep"
- **Decision.** In `host_live_graph`, all search, filtering, lineage highlighting, node detail, and diff are answered by the **server running SQL** against `ancestree.db` (via `MetadataStore` and a query grammar compiled to SQL). The browser is a **thin client**: it fetches a lightweight graph skeleton for layout and asks the server for everything else on demand. The full metadata blob is no longer shipped to, or filtered in, the browser.
- **Rationale.** Metadata now lives in SQL. Re-inlining it and re-implementing a query language in JavaScript (today's client-side "grep") duplicates the Python query engine and does not scale — 10k richly-annotated nodes is a multi-MB inlined blob. One query grammar (Python → SQL) now serves *both* `store.find(...)` and `/api/search`, deleting the parallel JS implementation in live mode.
- **Static export → view-only snapshot (decided).** An offline single-file HTML has no server to query, so rather than re-implement the query grammar in JavaScript, the static export becomes a **view-only snapshot**: the lineage graph plus click-to-view metadata, with **no search/query box**. This yields exactly one query implementation (Python → SQL) and removes the JS query parser entirely; rich search/filter/diff live in the server (`host_live_graph`). This revises AD8.
- **Trade-off.** Rich live search requires the server to be running (expected for the live mode). If the static export goes view-only, the emailable file becomes a shareable snapshot rather than a search tool.

---

## 5. Target Architecture & Directory Layout

### 5.1 Package layout

```
src/ancestree/
├── __init__.py               # public API surface (LineageStore, __version__)
├── __main__.py               # CLI: python -m ancestree serve|export|compact|migrate
├── py.typed
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
│   ├── fingerprint.py        # node content-identity (hash + equality) → node-level dedup
│   └── provenance.py         # who/what/how capture (user, python, platform, git)
│
├── db/                       # how state is PERSISTED — all SQLite
│   ├── __init__.py
│   ├── connection.py         # ConnectionManager: pragmas, per-thread/PID conns, write lock, fork reset
│   ├── schema.py             # canonical DDL + ensure/verify + user_version migrations
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
        ├── template.html
        ├── actions.js
        ├── styles.css
        └── vis-network.min.js
```

The root holds only entry points (`store`, `__main__`) and cross-cutting leaves (`maintenance`, `errors`, `util`); each package is one architectural layer. During the transition the 0.1.x modules (`core.py`, `models.py`, `database.py`, `chunkstore.py`, `utils.py`, `vis.py`) sit flat at the root beside them — an easy visual cue for what is legacy — and are deleted in Phase 9.

### 5.2 On-disk layout (at rest)

```
<root>/
  ancestree.db (+ -wal, -shm)   # the entire durable store
  .scratch/<node_id>/…          # exists only mid-write, then ingested and deleted
  interactive_pipeline.html     # static export, on demand
```

The reassembled-artifact **read cache** lives in the system temp directory (via `tempfile`), *not* under the store root: hard-kill leftovers become the OS temp-cleaner's problem (zero reaping code), and the root never accumulates derived data — at rest the store is just the database.

### 5.3 File-by-file — contents & rationale

#### Top level — entry points & cross-cutting leaves

**`__init__.py`** — *Contains:* re-exports of `LineageStore` and `__version__`. *Why:* one stable import point that hides the internal reshuffle.

**`__main__.py`** — *Contains:* an `argparse` CLI — `python -m ancestree serve|export|compact|migrate <root>`. *Why:* the live server and the migration tool should be usable without writing Python; stdlib-only (HC1).

**`store.py` — `LineageStore` (facade).** *Contains:* construction/wiring of `ConnectionManager`, `MetadataStore`, `ChunkStore`, `RuleEngine`, `Pruner`; the context-manager; and thin delegating methods (`create_node`, `get`/`find`/`latest`/`lineage`/`children`/`ancestors`, `prune`, `compact`, `sql`, `stats`, `export`, `generate_web_graph`, `host_live_graph`). *Why:* the current 899-line God-class should *orchestrate*, not *implement*. Every algorithm moves to a focused module.

**`maintenance.py`.** *Contains:* `Pruner.prune(node_id, dry_run=True)` (DAG-aware: a child dies only if all parents die — expressed with SQL + `ON DELETE CASCADE`); `compact()` (delete chunks unreachable from any artifact or as a live delta base — a one-hop closure under depth-1 — then `PRAGMA incremental_vacuum`, with full `VACUUM` as an explicit deep-compact option); and the **orphan-scratch sweep** run at store open (adopt a dead process's seeded scratch as an unhealthy node — §7.5). *Why:* destructive/space ops separated from the read/write path, top-level because they cut across `db/` and `ingest/`; unifies the `gc`/`flush`/`compact` vocabulary into one verb.

**`errors.py`.** *Contains:* `AncestreeError` base + `InvalidTransition`, `NodeNotFound`, `SchemaError`, `IntegrityError`, `CorruptChunkError`. *Why:* replaces scattered bare `ValueError`/`RuntimeError`/`PermissionError` so callers can catch selectively.

**`util.py`.** *Contains:* `to_jsonable` (numpy/pandas duck-typed coercion — keeps pandas optional), ISO time parse/format, misc helpers. *Why:* one home; retires the `parse_time`/`parse_iso_utc` split.

#### `domain/` — the vocabulary of lineage (pure logic, no I/O)

**`domain/node.py` — `Node` (record) + recording handle.** *Contains:* the immutable **`Node`** returned by queries (identity + `metadata` property, `artifacts`, read-side `/`, `__repr__`), and the mutable **recording handle** yielded by `create_node` (write-side `/` and `add_meta`). Both read metadata from `MetadataStore` and artifacts from `ChunkStore`/`NodeWorkspace`. *Why:* separating the read record from the write handle prevents `add_meta` on a queried node and lets the record be an immutable, hashable value object — see [AD10](#ad10--redesign-the-public-api-for-coherence). A node is a thin handle over the database, not a directory.

**`domain/metadata.py`.** *Contains:* the `{value, data_type, group, searchable}` envelope builder — validation, reserved-key guard, type inference (`auto` → table/json/image/link/text), pandas-DataFrame → `{columns, rows}` coercion, and `to_row()` (extract `num_value` for indexing). *Why:* one home for envelope logic currently tangled inside `Node._set_meta`.

**`domain/rules.py` — `RuleEngine`.** *Contains:* `validate(step_type, parent_step_types)` (raises `InvalidTransition`) and `generation_for(step_type, parents)`. *Why:* pure, unit-testable transition/generation logic lifted out of `create_node`.

**`domain/fingerprint.py`.** *Contains:* `content_hash(step_type, parent_ids, content_meta, artifact_digests)` and `content_equal(a, b)`. *Why:* node-level dedup is content-identity logic, not node behaviour; extracting it lets it be tested in isolation.

**`domain/provenance.py`.** *Contains:* `capture()` → user / python_version / platform / git_commit / git_branch / git_dirty. *Why:* already self-contained; a clean move from `utils.py`.

#### `db/` — persistence

**`connection.py` — `ConnectionManager`.** *Contains:* opens the DB with `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout`, `mmap_size`, `temp_store=MEMORY`; hands out thread-local read connections; exposes a serialized, **reentrant** `write()` transaction (single writer; nested blocks join the outermost transaction, so ingest commits node row + chunks + artifacts as one atomic unit); rebinds connections if the PID changed (fork-safety, replacing `_reset_workers_after_fork`); checkpoints the WAL after large ingest transactions so the sidecar never balloons. *Why:* SQLite's threading/fork rules are the trickiest correctness surface and deserve one owner.

**`schema.py`.** *Contains:* `SCHEMA_SQL`, `SCHEMA_VERSION`, `ensure_schema(conn)`, `migrate(conn)` (steps `PRAGMA user_version`). *Why:* the DDL must live in one canonical, versioned place; the current code has no schema at all.

**`metadata_store.py` — `MetadataStore`.** *Contains:* `add_node` (node row + edges + metadata rows in one txn), `get`, `exists`, `find(**kwargs)`, `most_recent`, `children`, `lineage` (recursive CTE), `find_by_hash`, `remove`, `all_node_ids`. *Why:* replaces `lineage_database` and *all* its snapshot/journal/reconcile complexity; SQLite provides durability, concurrency, and indexing. Multi-key `find` compiles to per-key `INTERSECT` over the metadata table — the trickiest SQL in the build, deliberately confined to this one well-tested method.

**`chunk_store.py` — `ChunkStore`.** *Contains:* `put_chunk(plaintext)` (exact dedup → resemblance/delta or raw), `get_chunk(digest)` (for a delta, fetch its raw base — depth-1 — decode, verify SHA-256), `add_artifact`, `artifact_manifest`, `reassemble` (into the read cache), `has_artifacts`, `gc`. Also owns the session **read cache**: a `tempfile`-managed directory in the *system temp dir* that `reassemble` writes into — folded in here because reassembly is its only consumer, and living in system temp makes hard-killed leftovers the OS temp-cleaner's problem (replacing the old `fcntl` lock/reap machinery with nothing). *Why:* absorbs the filesystem chunk pool + `Node`'s packing/reassembly/manifest, now SQLite-backed and driving the two-layer dedup.

#### `ingest/` — the write path (scratch → chunks → SQLite)

**`ingest/workspace.py` — `NodeWorkspace`.** *Contains:* create the scratch dir, resolve `node / "x"` write targets inside it (with the escape-guard), enumerate written files, and drive ingest → delete on block-exit. Writes a tiny **seed file** (`node_id`, `step_type`, parents, pid) at block-start so a hard-killed block's scratch can be adopted later (§7.5). *Why:* isolates the *only* remaining filesystem-write logic in one place, keeping `Node` free of I/O concerns (AD2).

**`ingest/cdc.py` — chunking + advanced dedup, one module.** *Contains three clearly-sectioned parts (~250 lines total):* **(1) FastCDC chunker** — `chunk(data) -> Iterator[bytes]`, Gear table, normalized masks, retunable min/avg/max constants (a larger min-size raises throughput at a small dedup cost), plus the fixed-boundary fallback for artifacts above the large-file threshold (AD4); **(2) resemblance** — `super_features(...)` computed in the chunking pass, backing the `chunk_feature` lookup for candidate bases (Layer 2 discovery); **(3) delta codec** — `encode(base, target)` / `decode(base, blob)` via `zlib` dictionary compression with the base chunk as `zdict` (AD5), ~20 lines at C speed, no bsdiff/xdelta/zstd (HC1). *Why one module:* these are three faces of a single algorithmic concern with ~250 lines between them — a three-file package would fragment what today's `chunkstore.py` already proves reads well as one file. Layer 1 is lifted from the existing implementation with reproducible boundaries.

**`ingest/packing.py`.** *Contains:* the ingest pipeline — read a node's scratch files, chunk via `cdc`, dedup/delta via `ChunkStore`, write chunk + artifact rows in one transaction, compute artifact SHA-256s + node size + `content_hash` in the same pass, mark scratch for deletion. Runs synchronously by default; the same entry point can be dispatched to a single worker thread if `background=True` (AD4). *Why:* one place for the write→durable transform, decoupled from both `Node` and the storage primitives.

#### `web/`

**`graph.py`.** *Contains:* `build_graph(store) -> {nodes, edges, levels}` — the lightweight lineage *skeleton* (ids, step_type, generation, edges) for layout, sourced from `MetadataStore`. Full per-node metadata is fetched on demand (live server) or inlined once (static export). *Why:* the `visualise_nodes` logic, decoupled from storage; keeps the live payload small (AD11).

**`export.py`.** *Contains:* `export_static(store, dest) -> Path` — render a **view-only** single-file HTML (lineage graph + click-to-view metadata, no query box; AD11) via named-placeholder templating, materializing referenced artifacts (or inlining small images as data-URIs) so links resolve now that bytes live in SQLite. *Why:* replaces the brittle exact-string asset inlining and the on-disk-link assumption; the searchable explorer is the live server.

**`server.py`.** *Contains:* `serve(store, port=0) -> url` — a `127.0.0.1` `http.server` serving the static assets plus a JSON API backed by **server-side SQL**: `/api/graph` (lightweight skeleton for layout), `/api/node/<id>` (full metadata on click), `/api/search?q=…` (the query grammar compiled to SQL via `MetadataStore`), `/api/diff?a=…&b=…` (both nodes pulled from SQL, diffed in Python), `/api/artifact/<id>/<rel>` (reassembled on demand). The browser is a thin client — no metadata blob is inlined and no client-side query engine runs. **Invariant:** every endpoint resolves by database key (`node_id`, `relpath`) — never by filesystem path — which makes path traversal structurally impossible; keep it that way. *Why:* implements `host_live_graph` (AD8) with direct Python/SQL querying (AD11) instead of in-browser grep.

**`assets/`.** The front-end. `template.html` cleaned of the `../../web_app/...` paths. In **live mode** `actions.js` is a thin client — search, diff, and node detail come from the server's SQL-backed API (AD11), with no client-side query engine. The client-side query engine is removed entirely: live mode uses the server API, and the static export is a view-only snapshot (AD11).

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

- **Frozen dataclasses for value objects** (`NodeRecord`, the metadata envelope, chunk records) — stdlib, self-documenting, and they enforce the immutability the record/handle split (AD10) promises.
- **Only `ingest/workspace.py` writes the filesystem.** Everything else goes through SQLite — a greppable architectural guarantee.
- **Server endpoints resolve by database key, never by filesystem path** — path traversal stays structurally impossible.
- **The subtle SQL is confined:** multi-key `find` (per-key `INTERSECT`) lives in one well-tested `MetadataStore` method; GC reachability lives in `maintenance.py`.
- **Layer packages, lean root.** The root holds entry points and cross-cutting leaves (`store`, `maintenance`, `errors`, `util`, `__main__`); everything else lives in `domain/`, `db/`, `ingest/`, or `web/`. Resist both God-classes and one-file-per-40-lines fragmentation.

---

## 6. Data Model — SQLite Schema

One database, `ancestree.db`, in WAL mode. Structural and provenance fields are promoted to columns (AD6); the `metadata` table holds only user metadata; chunks, deltas, and resemblance features live alongside.

```sql
PRAGMA user_version = 1;
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
-- Set at creation time (it cannot be enabled later without a full rewrite):
-- lets compact() reclaim space via incremental_vacuum instead of a full VACUUM.
PRAGMA auto_vacuum = INCREMENTAL;

-- Store-wide configuration: rules, gen_triggers, dedupe/chunk policy, format version.
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                       -- JSON
);

-- One row per node. Structural + provenance facts are real columns.
CREATE TABLE node (
    node_id         TEXT PRIMARY KEY,         -- 8-char id
    step_type       TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    created_utc     TEXT NOT NULL,            -- ISO-8601
    created_epoch   REAL NOT NULL,            -- most_recent / colour-by-time
    healthy         INTEGER NOT NULL,         -- 0/1
    duration_s      REAL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT,                      -- node-level dedup bucket key
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
    ordinal   INTEGER NOT NULL,               -- preserves parent order
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX idx_edge_parent ON edge(parent_id);   -- fast children() lookup

-- User metadata only. Envelope preserved; value is JSON so it holds scalars,
-- lists, dicts, or table blobs. num_value backs fast numeric range queries.
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

-- Content-addressed chunk pool. Each chunk stored once (INSERT OR IGNORE = dedup).
-- A chunk is raw (zlib) or a delta: zlib dictionary-compressed against base_digest
-- (Layer 2). Depth is capped at 1: a base_digest always names a RAW chunk (kind=0).
CREATE TABLE chunk (
    digest        TEXT PRIMARY KEY,            -- sha256 of the ORIGINAL plaintext chunk
    kind          INTEGER NOT NULL,            -- 0 = raw(zlib), 1 = delta (zlib zdict)
    base_digest   TEXT REFERENCES chunk(digest),
    data          BLOB NOT NULL,               -- zlib(raw) or the delta stream
    length        INTEGER NOT NULL,            -- plaintext length
    created_epoch REAL NOT NULL
);

-- Resemblance index: super-feature → chunk, for near-duplicate discovery.
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
    sha256  TEXT NOT NULL,                     -- whole-artifact digest (integrity + dedup)
    PRIMARY KEY (node_id, relpath)
);

-- Ordered recipe: which chunks reconstruct each artifact.
CREATE TABLE artifact_chunk (
    node_id TEXT NOT NULL,
    relpath TEXT NOT NULL,
    ordinal INTEGER NOT NULL,                  -- chunk order within the artifact
    digest  TEXT NOT NULL REFERENCES chunk(digest),
    PRIMARY KEY (node_id, relpath, ordinal),
    FOREIGN KEY (node_id, relpath) REFERENCES artifact(node_id, relpath) ON DELETE CASCADE
);
CREATE INDEX idx_ac_digest ON artifact_chunk(digest);   -- reachability / GC
```

**Relationships in one breath:** `node` 1─N `metadata`; `node` N─N `node` via `edge` (the DAG); `node` 1─N `artifact` 1─N `artifact_chunk` N─1 `chunk`; a `chunk` may reference another `chunk` (`base_digest`) for delta storage and has N `chunk_feature` rows for resemblance lookup. **Deduplication lives at two keys:** `chunk.digest` (identical chunk ⇒ same row) and `node.content_hash` (identical node ⇒ reused row).

**GC reachability note.** A chunk is live if it is referenced by any `artifact_chunk` **or** is the `base_digest` of a live delta chunk — a single-hop closure, since delta depth is capped at 1 (AD5). `compact()` computes this before deleting, so a delta's base is never reaped out from under it.

**Storage notes.** Chunks are ≤256 KB, comfortably inside SQLite's BLOB limits. `auto_vacuum=INCREMENTAL` is set at creation (it cannot be enabled on an existing database without a full rewrite), so `compact()` returns freed pages to the OS without rewriting the whole file; full `VACUUM` remains available as an explicit deep-compact.

---

## 7. Runtime Behaviour & Data Flow

### 7.1 Three-tier storage model

| Tier | Location | Role & speed |
|------|----------|--------------|
| **Hot (write buffer)** | `.scratch/<id>/` plain files | Where user code writes. Native OS speed; same-session reads are native. |
| **Durable + queryable** | `ancestree.db` (mmap'd) | Metadata: tiny synchronous WAL commit. Chunks: written at ingest (sync by default). Loaded via memory-map, queried via indexes. |
| **Read cache** | system temp dir (`tempfile`), plain files | First read of a packed artifact reassembles once; subsequent reads are native. OS-cleaned; never under the store root. |

SQLite is the *durable store and query engine*, never the synchronous hot path for artifact bytes.

### 7.2 Write path

1. `create_node` validates the transition (`RuleEngine`), computes the generation, allocates a `node_id`, and creates `.scratch/<node_id>/`.
2. User code writes files via `node / "x"` (native) and attaches metadata via `add_meta`.
3. On clean block-exit: `packing` reads scratch files in one pass — chunk (`cdc`), dedup/delta (`ChunkStore`), and compute artifact SHA-256s + total size + node `content_hash`.
4. If `dedupe` and an identical `content_hash` exists (byte-verified), the node is *adopted* (the handle rebinds to the existing row; scratch is discarded).
5. Otherwise one transaction writes the `node` row, `edge` rows, `metadata` rows, `chunk`/`chunk_feature` BLOBs, and `artifact`/`artifact_chunk` rows.
6. Scratch is deleted.

### 7.3 Read path & artifact resolution

Each node has its **own** scratch directory (`.scratch/<node_id>/`) — there is no shared scratch file, so multiple nodes in a session never interfere. A node's scratch is deleted once its artifacts are ingested (at its block-exit, in the synchronous default). Reading a logical artifact `(node_id, relpath)` always resolves by the same precedence:

1. **Loose scratch file** — if it still exists (the node's block hasn't ingested/cleared it yet). Native read, no reassembly.
2. **Read cache** — `<system-temp>/ancestree-<session>/<node_id>/<rel>`; reused if already reassembled this session.
3. **Reassemble from SQLite** — fetch the artifact's chunks, decode any delta chunks against their raw bases (depth-1), verify SHA-256, write into the read cache, and return that path.

So reads of what a node just wrote *inside its own block* are native (scratch); once ingested — including every read of an *earlier* node later in the same session — bytes come from SQLite via the cache. `node / "x"` and `node.artifacts()` re-resolve through this precedence on every call, so **do not hold a raw scratch `Path` across the block boundary** (that file is deleted at ingest) — re-fetch through `node / "x"` / `artifacts()` and you always get a valid path. The read cache is wiped at session end.

### 7.4 Query path

`find_node` / `get_most_recent_node` compile to indexed `WHERE` clauses; `get_lineage` is a recursive CTE; `get_child_nodes` is an indexed `edge` lookup. Lambda predicates run in Python over the SQL-narrowed set.

### 7.5 Crash & concurrency semantics

- **Clean block-exit** ⇒ node + artifacts committed atomically; durable and visible to other processes immediately.
- **Python exception in the block** ⇒ partial scratch is ingested with `healthy=False` and remains searchable (forensic semantics preserved).
- **Hard kill / power loss mid-block** ⇒ the block's scratch survives with its **seed file** (`node_id`, `step_type`, parents, pid — written at block-start). The next store open sweeps `.scratch/`: any seeded directory whose owning process is dead is **adopted as an unhealthy node** (files ingested, `healthy=0`), so partial work stays evidence even across a SIGKILL — *stronger* than 0.1.x, which loses hard-kill partials entirely. Unseeded or empty leftovers are discarded, so scratch litter never accumulates.
- **Concurrent processes** ⇒ WAL gives many readers + one writer; the write lock and `busy_timeout` serialize writers. (Heavy multi-process write concurrency remains a SQLite limitation.)

---

## 8. What Gets Deleted — The Simplification Payoff

Removed outright by AD1–AD3 and AD6:

- The disposable-index subsystem: `.index.json`, `.index.log`, `_reconcile`, `_replay_log`, `_write_snapshot`, `_is_stale`, `_refresh_if_stale`, `rebuild_from_disk`, and the O(N) cold-start directory reconcile.
- The background-packer state machine: `_pack_queue`, `_pack_worker`, `_pack_lock`, `_enqueued`, `_reclaim_pending`, `_scan_done`, `_live_stores`, `os.register_at_fork`, `_reset_workers_after_fork`, `_scan_stragglers*` *(default configuration)*.
- All five `*.tmp` + `replace()` atomic-write dances, and the `.gc.lock` `O_EXCL` file + 60-second grace window.
- The loose-vs-packed reclaim ordering: `_reclaim_loose`, `_resolve`, and the "loose files until recipe durable" invariant.
- Per-node directories, `Node.path`, `_artifact_rels`, and the `rglob` walk duplicated across five methods.
- The system-vs-user key bookkeeping: `_system_keys`, `_RESERVED_KEYS`, `_PROVENANCE_KEYS`, `_NON_CONTENT_KEYS`.
- `flatten_meta`, `to_db`, and most of `is_match` (→ SQL).
- The `ReadCache` `fcntl` lock + `_reap_dead_sessions` loop (→ `tempfile` lifecycle).
- Three Node constructors + `_hydrate` (→ `new()` / `from_row()`).
- Documentation drift: the phantom `compact()` references, the `dedupe`/`chunk` signature-vs-docstring contradiction, the `parse_time`/`parse_iso_utc` split.

**Net effect:** the subtlest ~40% of the current code is deleted rather than moved. `LineageStore` and `Node` become thin; `database.py` disappears; `chunkstore.py`'s filesystem plumbing collapses into transactional SQL.

---

## 9. Trade-offs & Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Single-DB-as-truth** (AD9) | Corrupt `.db` = data loss; no file fallback | WAL, `PRAGMA integrity_check`, periodic backup, on-demand `export()` to `meta.json` sidecars |
| **Pure-Python CDC throughput** (~3–10 MB/s Gear loop) | Slow ingest of large artifacts; synchronous block-exit pause | Runs after user code; fixed-boundary fallback keeps huge ingests at C speed (AD4); `background=True` seam; benchmark block-exit time |
| **SQLite on NFS** | Unreliable file locking | Documented non-goal; recommend local disk; `busy_timeout` for local multi-process |
| **Packed-read latency** | First read of a packed artifact costs reassembly | Loose scratch for recent reads; read cache for repeats; delta depth capped at 1 with a C-speed codec (AD5); Layer 2 gated by benchmark |
| **Multi-process write concurrency** | SQLite serializes writers | WAL + `busy_timeout`; acceptable for the single-writer common case |
| **Backward incompatibility** | Existing file-based stores won't open | One-time migration tool (Phase 9) reading old node dirs → new `.db` |
| **Rewrite risk** (rebuild + API break + test rewrite land together) | Regressions while the safety net is weakest | Old modules and tests stay green until Phase 9; migration round-trip-tested against a real 0.1.x store before any deletion |

---

## 10. Implementation Roadmap

Each phase is independently testable and leaves the suite green. Exit criteria reference the existing test files where possible. Tick items as they land.

**Safety-net policy:** the 0.1.x modules and their tests stay in place and green until Phase 9 — the new package grows alongside them, and nothing is deleted until the migration tool round-trips a real old store.

### Phase 0 — Scaffolding
- [x] Create the new package skeleton (empty modules per [§5.1](#51-package-layout)) alongside the current code.
- [x] Add this blueprint to the repo; set up a `rebuild` tracking branch.
- [x] Confirm CI (ruff, mypy `strict`, pytest) green on unchanged code.
- **Exit:** skeleton imports; CI green.

### Phase 1 — Persistence foundation
- [x] `db/connection.py` — pragmas, per-thread/PID connections, write lock.
- [x] `db/schema.py` — DDL from [§6](#6-data-model--sqlite-schema), `ensure_schema`, `migrate` stub.
- [x] `errors.py` — pulled forward from Phase 2 (`SchemaError` backs schema verification).
- **Exit:** create/open a `.db`, schema verified, connection & fork-safety unit tests pass.

### Phase 2 — Metadata store & queries
- [x] `util.py`, `domain/provenance.py` (`errors.py` landed with Phase 1).
- [x] `db/metadata_store.py` — `add_node`, `find`, `lineage` (recursive CTE), `children`, `most_recent`, `find_by_hash`, `remove`.
- **Exit:** query/lineage tests ported (`test_querying_and_search.py`, `test_dag.py`) pass against SQLite.

### Phase 3 — CDC Layer 1, chunk store & sync packing
- [x] `ingest/cdc.py` chunker section (FastCDC lifted from `chunkstore.py`) + fixed-boundary large-file fallback (AD4).
- [x] `db/chunk_store.py` (SQLite BLOBs, exact dedup, reassembly, session read cache in system temp), `ingest/workspace.py` (incl. the scratch seed file).
- [x] `ingest/packing.py` — synchronous ingest at block-exit.
- **Exit:** `test_chunking.py` (exact-dedup parts) passes; artifacts round-trip through SQLite; round-trip property tests (random data & mutations) green.

### Phase 4 — Domain decomposition & facade
- [x] `domain/{metadata,rules,node}.py`, `store.py`.
- [x] Wire `create_node`, context-manager, `add_meta`, `artifacts`, `__truediv__`.
- [x] `store.sql()` read-only escape hatch (`query_only` connection) and `store.stats()` (counts, sizes, dedup ratio).
- **Exit:** the test suite — rewritten to the redesigned API ([§11](#11-public-api-and-migration)) — is green against the new backend (`test_store_api.py`, `test_models.py`, `test_node_creation_edge_cases.py`).

### Phase 5 — Node dedup & maintenance
- [x] `domain/fingerprint.py`; `content_hash` column; adopt/rebind on identical content.
- [x] `maintenance.py` — `Pruner` (SQL cascade prune), `compact()` (orphan-chunk GC + `incremental_vacuum`), orphan-scratch sweep at store open.
- **Exit:** `test_dedupe.py` fully green (completes the current feature branch's goal); prune/compact/sweep tests pass.

### Phase 6 — CDC Layer 2 (advanced dedup)
- [x] `ingest/cdc.py` resemblance + delta sections (super-features computed in the chunking pass; zlib-`zdict` codec); wire into `packing`/`ChunkStore` behind the `chunk` policy.
- [x] Enforce delta depth 1 (bases are raw); extend `compact()` reachability to live bases; round-trip property tests through the delta path.
- [x] **Benchmark gate:** measure dedup ratio and ingest/read overhead on near-duplicate fixtures (`store.stats()` supplies the instrumentation); set the Layer-2 default from the data (AD5). → **2.3× less storage for 1.07× ingest; default ON** (`benchmarks/RESULTS.md`).
- **Exit:** benchmark results recorded in the repo; delta/resemblance tests green; `test_adversarial.py` green.

### Phase 7 — Web static export
- [ ] `web/graph.py`, `web/export.py` — structured templating + artifact materialization.
- **Exit:** `test_vis.py` green; generated HTML opens offline with working artifact links.

### Phase 8 — Local live server
- [ ] `web/server.py` — `host_live_graph()`; SQL-backed endpoints (`/api/graph`, `/api/node`, `/api/search`, `/api/diff`, `/api/artifact`); on-demand artifact reassembly.
- [ ] `actions.js` live mode — thin client fetching from the API; no client-side query engine (AD11).
- [ ] `__main__.py` CLI with the `serve` subcommand (`python -m ancestree serve <root>`).
- **Exit:** server serves the skeleton and streams search/diff/detail via SQL on `127.0.0.1`; smoke tests pass.

### Phase 9 — Migration & release polish
- [ ] One-time migration tool: old node dirs + `meta.json` — including packed artifacts via the old manifest + `.chunks` pool — → new `.db`.
- [ ] Complete the CLI: `export`, `compact`, `migrate` subcommands.
- [ ] Update `README.md`/docs — SQLite, NFS caveat, live server, and the marketing claims ("no database" becomes "no database *server*"; the static export is a view-only snapshot).
- [ ] Version bump to **0.2.0** + CHANGELOG with the old→new API cheat-sheet; delete dead modules (`database.py`, old `chunkstore.py`, `core.py`, `models.py`, `vis.py`).
- **Exit:** migration round-trips a real old store (loose *and* packed artifacts); docs updated; dead code removed; CI green.

---

## 11. Public API and Migration

Backward compatibility is **not** a goal (pre-1.0). The API is redesigned for one coherent vocabulary; the only fixed points are the write/read *feel* (HC2) and zero dependencies (HC1). See [AD10](#ad10--redesign-the-public-api-for-coherence).

### Redesigned surface

**Creation & recording** — `store.create_node(step_type, parent=None)` yields a mutable recording handle: `handle / "file"` (write target) and `handle.add_meta(...)`. Unchanged in feel (HC2).

**Queries** (return the immutable `Node` record, or a list of them):

| New | Was | Notes |
|-----|-----|-------|
| `store.get(id)` | `get_node` | resolve one node |
| `store.find(**filters)` | `find_node` | equality/range in SQL; lambdas in Python |
| `store.latest(**filters)` | `get_most_recent_node` | most recent match |
| `store.lineage(node)` | `get_lineage` | ancestry, oldest first |
| `store.children(node)` | `get_child_nodes` | direct children |
| `store.ancestors(node, **filters)` | `find_in_lineage` | filtered ancestry |
| `store.from_parent(node, pattern)` | `from_parent` | unchanged |

**Node record** — `node.node_id`, `.step_type`, `.generation`, `.parent_id`, `.metadata`, `.artifacts(pattern)`, `node / "file"` (read → readable path). Immutable and hashable. `Node.path` is **removed**.

**Maintenance & output** — `store.prune(node, dry_run=True)`, `store.compact()` (replaces `gc`/`flush`/`clear_cache`), `store.export(dest=None)` (grep-able `meta.json` sidecars), `store.generate_web_graph()` (static HTML), `store.host_live_graph(port=0)` (local server).

**Power queries & introspection (new)** — `store.sql(query, params=())`: any read-only `SELECT` over the documented schema, on a `query_only` connection (the schema is a versioned public contract via `PRAGMA user_version` and `schema.py`); the natural first step toward DataFrame querying. `store.stats()`: node/chunk counts, raw vs stored bytes, dedup ratio, database size — makes the deduplication visible.

**CLI (new)** — `python -m ancestree serve|export|compact|migrate <root>` (stdlib `argparse`).

**Store policy** — `LineageStore(root, rules=None, gen_triggers=None, dedup=..., chunk=...)`; `dedup`/`chunk` are persisted at creation like `rules`, not re-passed per open.

**Removed** — `Node.path`, `rebuild_db_from_disk()`, `gc()`, `flush()`, `clear_cache()` (folded into `compact()` / automatic lifecycle).

### Migration from 0.1.x

The on-disk format changes (per-node directories → `ancestree.db`), so this is a breaking change to *both* the storage format and the API. The Phase 9 migration tool reads an old store (node dirs + `meta.json`) and writes a new `ancestree.db`. A short old→new API cheat-sheet ships with the release notes; the renames above are mechanical. The rebuild ships as **0.2.0** (pre-1.0, so a breaking minor release is legitimate).

---

## 12. Future Work

- **DataFrame SQL querying.** Store pandas DataFrames such that SQL can query into their contents (candidate designs: node-discovery-by-DataFrame-facts, full relational materialization, or on-demand `store.sql(...)` over stored tables). The schema's JSON `value` column and `data_type='table'` leave room; pandas stays optional.
- **Background CDC hardening.** Promote the `background=True` seam to a first-class, fork-safe worker if large-artifact profiles demand it.
- **Streaming huge artifacts.** Use `sqlite3.Connection.blobopen()` (Python 3.11+) for incremental chunk I/O without whole-chunk buffers, once 3.9/3.10 support is dropped.

---

## 13. Glossary

- **Node** — one step in a pipeline; a row in the `node` table with metadata, edges, and artifacts.
- **Artifact** — an output file a node produced; stored as an ordered list of chunks.
- **CDC (Content-Defined Chunking)** — splitting data at boundaries chosen by content (via a rolling hash), so edits shift only local chunks and near-identical files share most chunks.
- **Chunk** — a variable-length, content-addressed (SHA-256) unit of artifact bytes; stored once.
- **Delta chunk** — a chunk stored as zlib dictionary-compression against a similar *raw* base chunk (Layer 2; depth capped at 1).
- **Super-feature** — a min-wise hash summarizing a chunk, used to find similar chunks.
- **Scratch** — the transient `.scratch/<node_id>/` directory a node's artifacts are written into before ingest.
- **Read cache** — the ephemeral, session-scoped copies of reassembled packed artifacts, kept in the system temp directory (OS-cleaned, never under the store root).
- **Lineage** — the transitive ancestry of a node (a recursive walk over `edge`).

---

## Decision Log

| Date | Change |
|------|--------|
| 2026-07-07 | v1.0 — initial blueprint approved. Core decisions: SQLite single source of truth (metadata + chunks); nodes as rows not folders; SQLite-owned atomicity; synchronous CDC by default with `background` seam; two-layer dedup; structural/provenance as columns; SQL search; keep `/` ergonomics + static export, add local server; accept single-DB resilience posture. DataFrame SQL querying deferred. |
| 2026-07-07 | v1.1 — Dropped public-API backward compatibility as a constraint (pre-1.0). Added AD10 (API redesign); reframed HC3 (ergonomics over compatibility); rewrote §11 into a redesigned surface + 0.1.x migration. Existing tests will be rewritten to the new API. |
| 2026-07-07 | v1.2 — Added AD11: the live server answers search/diff/detail via server-side SQL (`MetadataStore`), browser as a thin client, no client-side "grep"; one query grammar (Python → SQL) shared by `store.find` and `/api/search`. Static-export search model left open (fully-searchable JS file vs view-only snapshot vs drop). |
| 2026-07-07 | v1.3 — Static export decided: **view-only snapshot** (option b) — graph + click-to-view metadata, no query box; JS query engine removed entirely, leaving one query grammar (Python → SQL). Updated AD8 + AD11. Clarified §7.3 artifact resolution (per-node scratch lifecycle; loose → cache → reassemble precedence). |
| 2026-07-07 | v2.0 — Architect review pass over the whole plan. **AD5 reworked:** the pure-Python copy/insert delta codec (would have been the slowest component in the build) is replaced by **zlib `zdict` dictionary compression** — C-speed, ~20 lines — with **delta depth capped at 1** (bases always raw; one-hop GC; ≤2 fetches per read), and Layer 2's default is now set by a **Phase 6 benchmark gate**. **AD4 honesty pass:** Gear-loop throughput corrected to ~3–10 MB/s and a **fixed-boundary large-file fallback** added so huge ingests stay at C speed. **Crash forensics promoted** from Future Work into the plan: scratch **seed file** + startup **orphan-scratch sweep** adopt hard-killed partial work as unhealthy nodes (stronger than 0.1.x). **Schema:** `auto_vacuum=INCREMENTAL` set at creation. **Roadmap:** maintenance moved from Phase 9 to Phase 5; Phase 9 is now migration & release (ships as **0.2.0**; README marketing claims updated). Considered and deliberately kept: chunks in SQLite, synchronous-by-default CDC, lambda predicates in `find`, view-only static export. |
| 2026-07-07 | v2.1 — Soundness-review refinements and additions. **Read cache relocated** to the system temp dir (OS-cleaned; the store root at rest is just the database). **Structure trimmed** (~26 → ~21 files): `readcache.py` folded into `db/chunk_store.py`; the `cdc/` package collapsed into one sectioned `cdc.py`. **Added:** `store.sql()` read-only escape hatch (schema becomes a versioned public contract), `store.stats()` (feeds the Phase 6 gate), a stdlib CLI (`python -m ancestree serve|export|compact|migrate`), and §5.5 code conventions (frozen dataclasses; only `workspace.py` touches the filesystem; DB-keyed server endpoints). **Hardening:** multi-key `find` = per-key `INTERSECT` confined to one method; WAL checkpoint after large ingests; explicit safety-net policy (old modules/tests stay green until Phase 9; new Rewrite-risk row in §9). AD1 gains the one-transactional-domain argument. |
| 2026-07-07 | v2.2 — Layered package layout (Phase 0 feedback: too many flat root modules). The root keeps entry points and cross-cutting leaves (`store`, `maintenance`, `errors`, `util`, `__main__`); everything else moves into layer packages: **`domain/`** (node, metadata, rules, fingerprint, provenance — pure logic), **`db/`** (unchanged), **`ingest/`** (workspace, cdc, packing — the write path), **`web/`** (unchanged). Root shrinks from ~19 flat modules to 6 (12 during the transition, while the 0.1.x modules coexist). §5.1/§5.3/§5.4/§5.5 and roadmap paths updated. |
| 2026-07-08 | v2.3 — **Layer-2 benchmark gate resolved (AD5): the `chunk` policy defaults ON.** On the target workload (12 near-duplicate versions, ~1% scattered in-place edits) Layer 2 stored **2.3× less** than Layer 1 alone (dedup ratio 2.31 vs 1.00) for 1.07× ingest and sub-millisecond read overhead — see `benchmarks/RESULTS.md`. Implementation notes: resemblance uses min-wise transforms over strided 8-byte samples (C-speed; a wrong candidate costs one trial encode, since a delta is kept only when it beats plain compression) rather than a per-byte rolling min-hash; the zdict is truncated to 32,256 bytes because zlib's usable match distance is `w_size − MIN_LOOKAHEAD` (32,506), so a full-32K dictionary leaves aligned content exactly out of reach. |

---

*This is a living document. Update [§4](#4-key-architectural-decisions) and the Decision Log whenever a decision changes, and keep the [Roadmap](#10-implementation-roadmap) checkboxes current.*
