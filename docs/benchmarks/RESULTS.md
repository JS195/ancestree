# Benchmark results

The performance record for ancestree 0.2.0. Every number below is output from
the two notebooks in this directory, which run end to end in a temp directory
and clean up after themselves:

- [`storage_benchmark.ipynb`](storage_benchmark.ipynb) — what deduplication saves
- [`timing_benchmark.ipynb`](timing_benchmark.ipynb) — where the time goes

Reproduce them with `pip install -e ".[dev]" jupyter matplotlib pandas` and
run both notebooks. The charts live there; this file is the tables.

**Measured on**

| | |
| --- | --- |
| ancestree | 0.2.0 |
| Python | 3.12.12 |
| Platform | macOS 26.5.1, arm64 |
| Chunker | min 8 KiB, avg 16 KiB, max 256 KiB; fixed cuts above 64 MiB |

Absolute times are machine-specific. The ratios, and the distance between one
row and the next, are what transfer.

**Two figures, not one.** `store.stats()` reports *logical bytes* (what the
artifacts would occupy as plain files) against *stored bytes* (what the chunk
pool holds). Their quotient is the dedup ratio. But the database file also
carries node rows, metadata, artifact-to-chunk recipes, the resemblance index
and every SQLite index. Where both are given below, `true_ratio` divides the
logical size by the **whole file on disk** — the honest number. Every storage
figure is taken after a WAL checkpoint, so nothing is counted twice.

---

## Storage

### 1. Compression alone, no duplicates

One 8 MB artifact of each type, in its own store, with nothing to deduplicate
against. This is the floor: zlib doing its ordinary job on individual chunks.

| type | chunks | logical MB | stored MB | ratio | whole-file zlib-6 | verbatim chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| csv | 419 | 7.813 | 2.950 | 2.648 | 2.762 | 0 |
| jsonl | 329 | 8.612 | 1.201 | 7.168 | 7.309 | 0 |
| log | 409 | 6.585 | 1.100 | 5.986 | 7.330 | 0 |
| float64 | 434 | 8.000 | 7.698 | 1.039 | 1.041 | 0 |
| int32 | 408 | 8.000 | 3.786 | 2.113 | 2.238 | 0 |
| sparse | 47 | 8.000 | 0.121 | 66.070 | 67.975 | 0 |
| random | 437 | 8.000 | 8.000 | 1.000 | 1.000 | 437 |
| repetitive | 32 | 8.000 | 0.025 | 316.313 | 342.826 | 0 |

Chunk-wise compression tracks whole-file compression closely but never beats
it: compressing 16 KiB at a time forfeits the long-range matches a single
stream would find. That is the price of addressable chunks, and it is what
buys deduplication across artifacts.

The `verbatim_chunks` column is the store declining to compress. zlib
*inflates* data it cannot compress, so a chunk that does not shrink is kept
as-is — no cost on write, no pointless decompress on every read. For
already-compressed formats (PNG, parquet, zip) that is nearly every chunk.

A practical corollary: `np.savez` plus this store beats `np.savez_compressed`,
because the uncompressed form chunk-dedups across versions and the compressed
one never will.

### 2. Layer 1 — exact chunk deduplication

The same 8 MB artifact written into ten separate nodes with
`reuse_identical=False`, so node reuse cannot short-circuit it and every copy
really goes through the write path.

| store | nodes | chunks | logical MB | stored MB | db MB | ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| csv ×1 | 1 | 427 | 7.813 | 2.951 | 3.902 | 2.648 |
| csv ×5 | 5 | 427 | 39.066 | 2.951 | 4.250 | 13.240 |
| csv ×10 | 10 | 427 | 78.132 | 2.951 | 4.676 | 26.480 |
| float64 ×1 | 1 | 434 | 8.000 | 7.697 | 8.578 | 1.039 |
| float64 ×5 | 5 | 434 | 40.000 | 7.697 | 8.934 | 5.196 |
| float64 ×10 | 10 | 434 | 80.000 | 7.697 | 9.383 | 10.393 |
| random ×1 | 1 | 430 | 8.000 | 8.000 | 8.871 | 1.000 |
| random ×5 | 5 | 430 | 40.000 | 8.000 | 9.219 | 5.000 |
| random ×10 | 10 | 430 | 80.000 | 8.000 | 9.641 | 10.000 |

The chunk count is flat from the second copy onward: every chunk is already in
the pool and the insert is ignored. Ten copies of an incompressible 8 MB file
cost 8 MB, not 80 MB — and that holds for `random`, where compression can do
nothing at all. This is the layer that matters most for the way people
actually work: copy a dataset into three branches, and one of them is stored.

### 3. Node reuse — the layer above

With `reuse_identical=True` (the default), an identical rerun does not create a
node at all. Ten identical reruns of the same 8 MB CSV:

| | |
| --- | ---: |
| nodes in store | **1** (of 10 attempted) |
| artifacts | 1 |
| stored | 2.95 MB |

Change any input, any metadata value or any artifact byte and you get a
distinct node. Failed runs never merge. Pass `reuse_identical=False` to record
every run.

### 4. Layer 2 — delta storage against near-duplicates

The hard case. Twelve successive revisions with ~1% of bytes overwritten each
time, scattered across the whole file. Content-defined boundaries survive the
edits so the chunks stay aligned, but almost every chunk differs by a byte or
two: exact dedup shares nothing, and delta storage has to earn its keep.

| store | chunks | logical MB | stored MB | db MB | ratio | zlib | delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| csv, layer 1 only | 2557 | 46.246 | 22.401 | 24.965 | 2.064 | 2557 | 0 |
| csv, layer 1+2 | 2545 | 46.253 | 11.540 | 14.898 | 4.008 | 864 | 1681 |
| float64, layer 1 only | 2579 | 48.000 | 46.385 | 49.219 | 1.035 | 2579 | 0 |
| float64, layer 1+2 | 2688 | 48.000 | 21.226 | 24.754 | 2.261 | 935 | 1753 |

Layer 2 stores **1.94× less** on the CSV (22.4 MB → 11.5 MB) and **2.19× less**
on the float64 array (46.4 MB → 21.2 MB).

The first revision costs full price either way — there is nothing to delta
against yet. What separates them is the slope: without Layer 2 every revision
adds most of an artifact; with it, each revision adds roughly the bytes that
actually changed.

Note `delta` against `zlib` in the last two columns. A delta is kept only when
it beats plain compression by a margin, and only non-delta chunks may serve as
bases (depth is capped at 1). Taking every marginal delta measurably *costs*
storage, because each one removes a candidate base from the resemblance index.

### 5. Edit patterns

Four ways an artifact changes between twelve revisions of a 4 MB CSV.
Scattered overwrites touch 1% of bytes spread across the file; the other three
concentrate the same volume at one point, either shifting everything after it
(insert, delete) or not (append).

| pattern | chunks | logical MB | stored MB | ratio | delta chunks |
| --- | ---: | ---: | ---: | ---: | ---: |
| overwrite | 5083 | 92.493 | 23.479 | 3.939 | 3266 |
| insert | 268 | 49.368 | 2.129 | 23.188 | 7 |
| delete | 239 | 43.345 | 1.650 | 26.272 | 7 |
| append | 246 | 49.361 | 1.964 | 25.135 | 10 |

The three concentrated patterns deduplicate almost perfectly and barely touch
Layer 2: the edit invalidates the handful of chunks around it, the boundary
algorithm re-syncs within a chunk or two, and everything after that hashes
identically to the previous revision. That re-sync is the whole point of
content-defined chunking, and `insert` and `delete` are the proof — a
fixed-block scheme would shift every block after the edit point and share
nothing beyond it.

Scattered overwrites are the genuinely hard case, and the one Layer 2 exists
for. The same 1% spread across the whole file leaves almost every chunk
differing by a byte or two, so the ratio rests on deltas instead: 3266 of them,
against 7 for `insert`. It still lands at a useful ratio, but it earns it the
expensive way.

### 6. Artifact size

Three revisions of one CSV, from below the minimum chunk size to across the
64 MiB threshold where the chunker stops looking for content-defined
boundaries and cuts at fixed offsets instead.

| artifact | path | chunks | logical MB | stored MB | db MB | ratio | true ratio | delta |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.016 MB | CDC | 6 | 0.040 | 0.007 | 0.121 | 5.492 | **0.328** | 4 |
| 0.062 MB | CDC | 9 | 0.165 | 0.029 | 0.145 | 5.701 | 1.143 | 6 |
| 0.250 MB | CDC | 37 | 0.676 | 0.166 | 0.316 | 4.073 | 2.138 | 18 |
| 1.000 MB | CDC | 150 | 2.802 | 0.702 | 1.016 | 3.992 | 2.759 | 66 |
| 4.000 MB | CDC | 626 | 11.561 | 2.859 | 3.836 | 4.044 | 3.014 | 278 |
| 16.000 MB | CDC | 2571 | 47.196 | 10.942 | 14.609 | 4.313 | 3.231 | 1217 |
| 72.000 MB | fixed cuts | 882 | 220.459 | 81.363 | 83.234 | 2.710 | 2.649 | 0 |

The pool ratio holds up even on tiny files, but it is the wrong column to read
there. Compare `true_ratio`: on a 16 KiB artifact the pool is tiny and the file
is not, because a database has a page size, a schema and index pages that exist
whether you store anything or not. The fixed cost swamps the payload and the
store is **larger** than the plain files would be.

Deduplication is a megabyte-scale mechanism. The two ratios converge as the
payload grows past that fixed cost, and by a few megabytes the pool number is a
fair summary of the file.

Above 64 MiB the chunker switches to fixed 256 KiB cuts to keep huge ingests at
C speed. Exact whole-file and repeated-content dedup still work; the
shift-resilience does not, so an inserted byte near the front of a 100 MB file
rewrites the whole thing. That trade is deliberate.

### 7. What the database costs on top

Everything above quoted the chunk pool. Page accounting for a 12-revision CSV
store, via `dbstat`:

| object | KiB | share |
| --- | ---: | ---: |
| `chunk` (the payload) | 12404.0 | 88.9% |
| `sqlite_autoindex_chunk_feature_1` | 352.0 | 2.5% |
| `chunk_feature` | 312.0 | 2.2% |
| `artifact_chunk` | 240.0 | 1.7% |
| `sqlite_autoindex_chunk_1` | 204.0 | 1.5% |
| `idx_ac_digest` | 204.0 | 1.5% |
| `sqlite_autoindex_artifact_chunk_1` | 92.0 | 0.7% |
| `idx_feature` | 68.0 | 0.5% |

| | |
| --- | ---: |
| chunk payload | 10.27 MB |
| database file | 13.64 MB |
| overhead | 3.38 MB (33% on top of the payload) |
| ratio on the pool | 4.504 |
| **ratio on the file** | **3.389** |

The resemblance index (`chunk_feature`) is the largest non-payload consumer,
and it is what makes Layer 2 possible: a few rows per chunk to find delta bases
without comparing every chunk to every other. On a store dominated by artifacts
the overhead is a small percentage. On a metadata-only store there is no
payload at all, and the file is entirely index.

### 8. Metadata-only nodes

Most nodes in a real pipeline hold no artifacts. 2000 nodes with three metadata
entries each:

| | |
| --- | ---: |
| database | 5.38 MB |
| per node | **2820 bytes** |
| extrapolated to a million nodes | ~2.6 GB |

Provenance — user, platform, Python version, git commit, branch and dirty flag
— is the bulk of it, and is recorded on every node.

### 9. A mixed corpus

Six file types (csv, jsonl, log, float64, int32, random), six revisions each,
~1% of bytes edited per revision: 60% scattered overwrites, 20% insertions, 20%
deletions, so shift-resilience is genuinely exercised rather than assumed.

| policy | nodes | chunks | logical MB | stored MB | db MB | ratio | true ratio | delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `delta=False` (layer 1 only) | 6 | 3753 | 69.958 | 39.515 | 43.363 | 1.770 | 1.613 | 0 |
| `delta=True` (the default) | 6 | 3759 | 69.956 | 17.815 | 22.367 | **3.927** | **3.128** | 2271 |
| `delta=True, reuse_identical=True` | 6 | 3730 | 69.960 | 18.121 | 22.707 | 3.861 | 3.081 | 2184 |

The default policy stores **3.93× less** than the logical size, which is
**2.22× better than Layer 1 alone**. Counting the whole database file rather
than the pool, still **3.13×**.

> **On the 2.6× figure quoted elsewhere.** The README and CHANGELOG quote 2.6×
> for a corpus of this shape. That measurement was taken on a corpus of *real
> files* rather than the generated payloads here, and it scattered its
> insertions and deletions instead of applying each at a single point — which
> section 5 shows is by far the harder case. The corpus behind it is not in
> this repository, so 2.6× is not reproducible from these notebooks. Treat it
> as the conservative end of the range, and the numbers above as what this
> suite actually measures.

### 10. Reclaiming space

`prune(dry_run=False)` removes a branch's nodes and then compacts: chunks no
surviving artifact references are dropped and the pages are returned to the OS.
A delta base still in use survives even if its own node is gone.

| stage | nodes | chunks | logical MB | stored MB | db MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| before | 16 | 1730 | 32.000 | 30.792 | 33.992 |
| after dry run | 16 | 1730 | 32.000 | 30.792 | 33.992 |
| after prune | 12 | 1304 | 24.000 | 23.094 | 25.777 |

8.21 MB of database file reclaimed. The dry run changes nothing. The real
deletion shrinks the file in place, and there is no undo: after compaction the
bytes are gone, not merely unreferenced.

### Storage, by what you are doing

| situation | what it costs |
| --- | --- |
| Rerunning a step unchanged | nothing stored (node reuse) |
| Copying an artifact into branches | stored once (Layer 1) |
| Iterating on a large artifact | ~the changed bytes (Layer 2) |
| Already-compressed formats | stored verbatim — no ratio, no loss |
| Files under 8 KiB | one chunk; overhead dominates |
| Files over 64 MiB | fixed cuts, exact dedup only |
| Mixed realistic corpus | 3.93× on the pool, 3.13× on the file |
| Metadata-only node | ~2820 bytes |

Run `store.stats()` against your own data. The ratio depends entirely on what
you produce and how much of it repeats.

---

## Timing

Each figure is the median of repeated runs after a warm-up, so a single
scheduling hiccup cannot set the number. Operations that consume the state they
measure (store creation, prune, the first cold open) are timed once.

### 1. Store lifecycle

| operation | ms |
| --- | ---: |
| `LineageStore(...)` create empty store | 2.829 |
| open + close existing store (200 nodes) | 0.183 |

Neither replays an index, which is why a cold open does not care how many nodes
are in the store.

### 2. Node creation

| operation | ms |
| --- | ---: |
| `create_node`, 1 metadata entry | 8.389 |
| `create_node`, 10 metadata entries | 8.413 |
| `create_node`, 50 metadata entries | 8.831 |
| — of which provenance capture (2× `git`) | 7.257 |

Fixed cost per node ~8.39 ms, of which provenance is **87%**. Marginal cost per
metadata entry ~9.0 µs — cheap and linear; the fixed cost of the block
dominates until you attach dozens.

Provenance is deliberately not cached: the worktree can be committed between
two nodes, and a stale `git_dirty` would misreport reproducibility.

### 3. Chunking throughput

The Gear rolling hash walking the bytes for content-defined boundaries. Pure
Python, no store, no compression, no database; 4 MB payloads.

| type | MB/s | chunks | avg chunk KiB |
| --- | ---: | ---: | ---: |
| log | 30.34 | 204 | 16.50 |
| csv | 26.02 | 209 | 18.88 |
| float64 | 25.32 | 215 | 19.05 |
| random | 25.08 | 210 | 19.50 |
| int32 | 24.90 | 208 | 19.69 |
| jsonl | 19.68 | 160 | 27.50 |
| sparse | 14.01 | 22 | 186.18 |
| repetitive | 13.69 | 16 | 256.00 |

Real data sits in a tight band around 25 MB/s. The outliers are slow for the
same reason: with too little byte variety the fingerprint almost never crosses
the boundary mask, so every chunk runs the full 256 KiB to the hard cut.

That is a throughput penalty, not a saving. The Gear loop skips the first
`MIN_SIZE` bytes of every chunk, because no boundary may fall there — at a
16 KiB average that skip covers half the payload, but at a 256 KiB chunk it
covers 3%, so almost every byte gets hashed. Degenerate data is both slower to
chunk *and* worse to store, because 16 chunks per 4 MB is far too coarse to
share anything. Watch the chunk count, not the megabytes per second.

### 4. The ingest stages

Per 4 MB of CSV. Chunking is one of four stages; each chunk is also hashed,
compressed, and fingerprinted for the resemblance index.

| stage | ms |
| --- | ---: |
| chunk (Gear boundaries) | 148.965 |
| zlib-6 every chunk | 147.810 |
| `super_features` every chunk | 17.150 |
| sha256 every chunk | 1.467 |
| `delta_encode` one chunk against a base | 0.806 |

Chunking and compression are the budget, in roughly equal share. Hashing is
free.

### 5. Write path by file type

The full round trip on 4 MB payloads, cold: what your code pays writing the
file natively, against what `create_node` costs end to end. Each type goes into
a fresh store, written once, so this is a cold ingest rather than a dedup hit.

| type | direct ms | store ms | overhead × | store MB/s |
| --- | ---: | ---: | ---: | ---: |
| log | 0.60 | 215.26 | 357.62 | 18.58 |
| random | 0.69 | 244.39 | 352.63 | 16.37 |
| float64 | 0.69 | 262.73 | 382.40 | 15.23 |
| jsonl | 0.84 | 321.68 | 382.55 | 12.43 |
| sparse | 0.68 | 334.90 | 495.35 | 11.94 |
| repetitive | 0.69 | 346.44 | 501.94 | 11.55 |
| csv | 0.74 | 352.49 | 475.56 | 11.35 |
| int32 | 0.69 | 552.75 | 806.54 | 7.24 |

The multiplier looks alarming because a direct write of a few megabytes is
nearly free — the OS takes the bytes and returns. What matters is the absolute
figure. Packing runs at tens of MB/s, so it is felt on artifacts measured in
hundreds of megabytes and invisible on the metadata-heavy nodes that make up
most of a pipeline.

### 6. Write cost against artifact size

A fresh store each time, so the previous run's chunk pool cannot contaminate
the next.

| artifact | ms | MB/s | path |
| ---: | ---: | ---: | --- |
| 0.25 MB | 32.49 | 7.70 | CDC |
| 1 MB | 91.10 | 10.98 | CDC |
| 4 MB | 353.38 | 11.32 | CDC |
| 16 MB | 1519.74 | 10.53 | CDC |
| 48 MB | 5009.46 | 9.58 | CDC |
| 80 MB | 6287.52 | 12.72 | fixed cuts |

Roughly linear, at a throughput that improves with size as the node's fixed
cost amortises away, then jumps above 64 MiB where the fixed-cut path runs at C
speed. Ingest is a megabytes-per-second budget: a quarter-megabyte artifact is
free, a 16 MB one is noticeable, and a hundred-megabyte one belongs at a step
boundary rather than inside a loop.

### 7. What deduplication costs and saves in time

A **node-level** hit is a whole rerun that produced identical content. A
**chunk-level** hit is a distinct node whose bytes are already in the pool.
Both against a cold 8 MB write into a store that has never seen the payload.

| operation | ms | vs cold |
| --- | ---: | ---: |
| cold write, 8 MB | 719.770 | — |
| chunk-level hit (same bytes, new node) | 328.165 | 2.19× faster |
| node-level hit (identical rerun) | 5.303 | **135.73× faster** |

A chunk-level hit still pays to read, chunk and hash the bytes — the store
cannot know they are duplicates until it has hashed them. What it saves is
compression and the write. A node-level hit additionally skips the insert
entirely and rebinds onto the node that already exists.

### 8. What Layer 2 costs

Eight successive revisions of a 2 MB float64 array, ~1% edited per revision.

| policy | ms | stored MB | ratio |
| --- | ---: | ---: | ---: |
| layer 1 only | 1130.0 | 15.44 | 1.036 |
| layer 1 + 2 | 1299.7 | 7.19 | 2.225 |

**1.15× the ingest time for 2.15× less stored.** That trade is why `delta`
defaults to on.

### 9. Read path

The first read of an artifact in a session reassembles it from chunks into the
session cache; every read after that is a plain file read.

| size | direct ms | cold ms | warm ms | cold × | warm × |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 MB | 0.038 | 3.010 | 0.062 | 79.20 | 1.63 |
| 4 MB | 0.110 | 10.675 | 0.130 | 97.01 | 1.18 |
| 16 MB | 0.990 | 42.034 | 0.913 | 42.45 | 0.92 |

Reassembly is a decompress and a concatenate, so it costs a small multiple of a
plain read — and only once per session. The warm number is a plain read plus a
node lookup, which is where the residual overhead lives.

### 10. Queries at scale

Searches are answered by indexed SQL, not by scanning nodes. Measured while a
3000-node store was being built.

| nodes in store | `find()` no filter, ms | `find(run_id=…)` selective, ms |
| ---: | ---: | ---: |
| 250 | 2.124 | 0.022 |
| 500 | 4.351 | 0.022 |
| 1000 | 8.852 | 0.022 |
| 2000 | 17.655 | 0.022 |
| 3000 | 26.860 | 0.023 |

A flat line for the selective lookup is the index doing its job. `find()` with
no filter has to materialise every node it matches, so it is expected to rise.

The full query surface over those 3000 nodes:

| operation | ms |
| --- | ---: |
| `get(node_id)` | 0.011 |
| `stats()` | 0.013 |
| `children(node)` | 0.019 |
| `find(run_id=…)` selective, 1 hit | 0.020 |
| `lineage(node)` 10-deep chain | 0.103 |
| `sql()` GROUP BY over node table | 0.201 |
| `find(dataset=…)` ~120 hits | 1.146 |
| `latest(step_type=…)` | 2.407 |
| `ancestors(node)` | 5.307 |
| `find(step_type=…)` ~1000 hits | 9.105 |
| `find(accuracy=predicate)` full scan | 11.108 |
| `find()` everything | 27.210 |

Building that store took 28.3 s — 9.42 ms per node, consistent with section 2.

### 11. Maintenance

Occasional operations, all of which touch the whole store and scale with it.
Measured on the 3000-node store.

| operation | ms |
| --- | ---: |
| `prune(node)` dry run | 0.225 |
| `prune(branch, dry_run=False)` | 1.683 |
| `compact()` on a clean store | 1.981 |
| `backup()` online copy | 5.872 |
| `integrity_check` via `sql()` | 13.073 |
| `export_metadata()` JSON sidecars | 515.344 |

`prune` defaults to a dry run, which is a graph traversal and nothing else. The
real deletion additionally compacts, which scans the whole chunk pool — so
pruning in a loop should pass `compact=False` and call `compact()` once at the
end.

### 12. Rendering

The static snapshot inlines the entire store into one HTML file, so its cost
scales with node count and artifact size.

| nodes | ms | output | per node |
| ---: | ---: | ---: | ---: |
| 100 | 5.614 | 542 KiB | 5555 bytes |
| 500 | 20.757 | 839 KiB | 1719 bytes |
| 2000 | 75.520 | 1953 KiB | 1000 bytes |

The per-node figure falls because the bundled `vis-network.js` is a fixed cost
paid once.

### Three cost classes

They are three orders of magnitude apart.

| class | operation | ms |
| --- | --- | ---: |
| **Microseconds** — free, put them anywhere | `get(node_id)` | 0.011 |
| | selective `find()` | 0.020 |
| | `lineage()` | 0.103 |
| **Milliseconds** — fine per pipeline step, not per row | `create_node`, metadata only | 8.389 |
| | `find()` over 3000 nodes | 27.210 |
| **Scales with your data** — budget for it | ingest 4 MB of CSV | 352.5 |
| | `export_metadata()` 3000 nodes | 515.3 |
| | ingest 48 MB artifact | 5009.5 |

The write path is the only thing that scales with data volume, and it is paid
at block exit rather than while your code runs. Everything on the read and
query side is indexed and effectively free at the scale a person explores at.
