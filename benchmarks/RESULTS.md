# Benchmark results

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
