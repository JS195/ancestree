# Benchmark results

## What ships: chunk encodings

A plain (non-delta) chunk is stored one of two ways:

| kind | encoding | when |
|-----|----------|------|
| 0 | zlib, level 6 | zlib makes the chunk smaller |
| 2 | plaintext, verbatim | it does not — the normal case for PNG, JPEG, parquet, zip, `.npz` |

Kind 1 is a zlib-zdict delta against a similar chunk (Layer 2, below).

Storing already-compressed payloads verbatim costs nothing and saves on
both ends: zlib *inflates* data it cannot compress (+1,536 bytes per 5 MB
of random input, measured) and every read paid a pointless decompress.
Nothing in the standard library shrinks a deflate or JPEG stream further.

A useful corollary: `np.savez` (uncompressed) plus this store beats
`np.savez_compressed`, because the uncompressed form chunk-dedups across
versions and the compressed one never will.

---

## Proposed, measured, NOT implemented: smallest-wins codecs

**This section describes a design that was evaluated and deferred. None of
it is in the code.** It is kept because the measurements were expensive and
the conclusion is worth having on record.

The proposal: store every plain chunk **smallest-wins** among verbatim,
zlib-9, lzma (FORMAT_RAW, fixed filters), bz2-9 and byte-shuffled lzma
(strides 2/4/8, blosc/HDF5-style) — all stdlib — and relax the delta-keep
margin with best-of-top-3 bases.

Measured on a 19.5 MiB corpus (6 file types x 6 revisions, ~1% edits):

| strategy                                   | stored KiB | ratio | write time |
|--------------------------------------------|-----------:|------:|-----------:|
| shipping (zlib-6, margin 0.8, best-1 base) |      6,274 |  3.18 |      3.1 s |
| zlib-9 instead of zlib-6                   |      6,251 |  3.19 |      4.9 s |
| smallest-wins codecs                       |      5,624 |  3.54 |     18.4 s |
| + delta margin 0.95                        |      5,603 |  3.56 |     18.4 s |
| + best-of-3 delta bases                    |      5,572 |  3.58 |     18.6 s |

**Rejected on write cost: −11% storage for ~6× ingest time.** Ancestree
runs inside someone's pipeline, where ingest time is felt on every step and
storage is not. zlib-9 alone is −0.4% for +60% compress time — not worth it
either.

Two further findings from the same work, both of which *did* ship:

- Relaxing the delta margin to 1.0 (take any delta that wins at all)
  measurably **costs** storage — ratio 2.63 → 2.59. Only non-delta chunks
  can serve as delta bases, so every delta taken removes a candidate base
  from the resemblance index. The margin is index policy, not caution.
- Storing incompressible chunks verbatim is free and strictly better.

Earlier per-file-type numbers for the codec proposal, on a different
corpus, are preserved in the git history of this file.

## Layer-2 gate (blueprint AD5 / Phase 6, issue #17)

`python benchmarks/layer2.py` — 12 successive versions of a 384 KiB
artifact, ~1% of bytes edited in place per version (scattered). This is
Layer 2's target workload: content-defined boundaries survive the edits,
but almost every chunk differs slightly, so exact chunk dedup (Layer 1)
shares nothing and resemblance/delta storage has to earn its keep.

Measured 2026-07-08 (Apple Silicon macOS, Python 3.12, `test-venv`):

| policy       | ingest_s | read_s | stored_KiB | dedup_ratio |
|--------------|---------:|-------:|-----------:|------------:|
| layer 1 only |    0.906 |  0.012 |       4610 |       0.999 |
| layer 1 + 2  |    0.966 |  0.018 |       1995 |       2.309 |

**Layer 2 stored 2.3× less than Layer 1 alone**, for 1.07× ingest time and
1.58× read time (0.012 s → 0.018 s across all 12 artifacts — noise in
absolute terms).

Re-measured 2026-07-25 at the shipping 16 KiB average chunk size:
**2063 KiB, ratio 2.23** — 3.4% *worse* than the 32 KiB size this was
originally measured at, and deliberately accepted. The payload here is `randbytes`: incompressible,
so smaller chunks buy nothing back and only multiply per-chunk row
overhead. On data that compresses at all the trade reverses sharply — see
below.

### Chunk size (measured 2026-07-25)

An average chunk size of 32 KiB — the first value tried — sat exactly on a cliff. zlib's usable
dictionary window is 32,256 bytes (`_ZDICT_MAX`), so a delta base at or
above the average chunk size cannot be seen in full and the tail of every
delta degenerates to literals — the same footgun recorded in the blueprint
changelog, one level up.

A 69 MB corpus of six file types (CSV, JSON, logs, float64 arrays, int32
arrays, an already-compressed blob) across six versions, each version
applying ~1% scattered edits — 60% overwrites, 20% insertions, 20%
deletions, so shift-resilience is genuinely exercised:

| MIN / AVG      | db bytes   | ratio | ingest |
|----------------|-----------:|------:|-------:|
| 8K / 32K       | 30,674,944 |  2.25 |  9.78s |
| 8K / 24K       | 27,484,160 |  2.51 |  8.34s |
| 8K / 20K       | 27,418,624 |  2.52 |  8.27s |
| **8K / 16K**   | **26,271,744** | **2.63** | **7.64s** |
| 8K / 12K       | 26,501,120 |  2.60 |  6.20s |
| 4K / 8K        | 15,802,368 |  4.37 |  6.93s †|

† in-place-edit-only corpus; not comparable to the rows above.

**16 KiB is −14.4% database bytes and −22% ingest time against 32 KiB.** It is
free in both directions because the Gear loop skips `MIN_SIZE` bytes per
chunk: halving the average size raises the skipped fraction from 25% to
50%, so chunking gets *faster*, not slower.

Two things measured and rejected on the same corpus:

- **Delta margin 0.8 → 1.0** (take any delta that wins at all): ratio falls
  2.63 → 2.59. Only non-delta chunks can serve as delta bases, so every
  delta taken removes a candidate base from the resemblance index. The
  margin is index policy, not caution about the delta.
- **Smallest-wins codecs** (trial-encode each chunk with lzma, bz2, a
  byte-shuffled lzma, keep the smallest): −11% stored bytes for ~6× ingest
  time. Rejected on the time budget; the design is preserved above.

### Decision (the AD5 gate)

The win is substantial on the target workload and the overhead marginal:
**the `chunk` policy defaults ON.** Stores that prioritise raw read speed
over storage can opt out at creation with `chunk=False` (persisted policy).

### Implementation notes affecting the numbers

- Resemblance features are min-wise transforms over strided 8-byte samples
  (C-speed per chunk), not a per-byte rolling min-hash — a deliberate
  trade: a wrong candidate only costs one trial encode, because a delta is
  kept solely when it beats plain compression by the policy margin.
- The zdict is truncated to 32,256 bytes: zlib's usable match distance is
  `w_size − MIN_LOOKAHEAD` (32,506), not the full 32,768-byte window, so a
  full-32K dictionary leaves same-aligned content exactly out of reach and
  silently degenerates every delta to plain compression. Chunks larger
  than the window delta imperfectly (their tails emit literals), which is
  why the first-copy-dominated 3-version test asserts a looser bound than
  the 12-version amortized ratio above.


## Performance pass (measured 2026-07-25, same machine)

Profiled rather than guessed. Two workloads: the 69 MB / six-file-type
corpus above, and 3000 metadata-only nodes. Storage output is identical
before and after — every change here is either provably equivalent or a
pure query-count reduction.

| operation                       | before | after | |
|---------------------------------|-------:|------:|-|
| write 69 MB across 36 artifacts | 7.65 s | 5.61 s | 1.36x |
| write 3000 metadata-only nodes  | 59.3 s | 27.3 s | 2.17x |
| `find()` over 3000 nodes        | 39.3 ms | 26.3 ms | 1.49x |
| `find(step_type=...)`           | 9.40 ms | 6.47 ms | 1.45x |
| `find(run_name=...)` selective  | 0.40 ms | 0.04 ms | 10x |
| explorer page render (3000)     | 144 ms | 115 ms | 1.25x |
| read every artifact back        | 0.22 s | 0.21 s | — |
| `find(acc=predicate)`           | 12.6 ms | 12.5 ms | — |
| `lineage()`                     | 0.06 ms | 0.06 ms | — |

Where the time went, and what changed:

1. **Provenance was 60% of node creation** (17 ms of ~19 ms). Every
   `create_node` spawned three `git` subprocesses. Two of them collapse
   into one (`rev-parse HEAD --abbrev-ref HEAD` answers both), and the
   remaining two run concurrently on a thread pool — they are
   subprocess-bound, so they overlap almost perfectly. 17 ms -> 6.3 ms,
   byte-identical output in a repo, in a fresh repo with no commits, on a
   detached HEAD and outside a repo. Deliberately **not** cached: the
   worktree can be edited or committed between two nodes, and a stale
   `git_dirty` would misreport reproducibility.

2. **The Gear loop was 60% of artifact ingest** (4.9 s of 8.2 s). The
   boundary tests only ever read the low `_MASK_S` bits of the fingerprint,
   and carries propagate upwards only — so the high bits cannot affect a
   boundary. Narrowing the Gear table to those bits keeps the arithmetic on
   single-digit CPython ints, and walking a slice replaces per-byte
   indexing. 16.6 -> 26.8 MB/s, boundaries **bit-for-bit identical**
   (guarded by a golden vector in the battle tests: boundaries are the
   storage format, and moving one silently breaks dedup against everything
   already stored).

3. **`find`/`lineage`/`ancestors` were N+1.** Resolving a list of ids ran
   two queries per node. `get_many` does it in two per batch.

4. **The explorer page render was 4 queries per node.** Now three for the
   whole store, sharing one detail builder with the per-node path so the
   two cannot drift.

5. **`idx_meta_key` covers `(key, value)`, not just `key`.** An equality
   find becomes an index seek instead of a scan of every row sharing the
   key. Costs ~2% file size.

6. **`super_features`** extracts its strided samples with a zero-copy
   `memoryview` cast on little-endian machines (1.44x), keeping the
   explicit loop as the portable fallback — features are persisted, so both
   paths are asserted equal.

Measured and **rejected**: `PRAGMA cache_size` at 64 MB (18.7 ms vs 18.8 ms
reading 300 artifacts — `mmap_size` already bypasses the page cache, and
turning mmap *off* costs 10%, so the existing pragma set is already right).
