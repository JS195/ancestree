"""The Layer-2 benchmark gate (blueprint AD5 / Phase 6, issue #17).

Measures what the resemblance/delta layer actually buys on its target
workload — successive near-duplicate versions of an artifact (scattered
in-place edits, so Layer 1's content-defined boundaries survive but almost
every chunk differs slightly) — and what it costs in ingest and read time.
The store's default `chunk` policy is set from these numbers; results are
recorded in benchmarks/RESULTS.md.

Run:  python benchmarks/layer2.py
"""

import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from ancestree.store import LineageStore

VERSIONS = 12
SIZE_BYTES = 384 * 1024
EDIT_FRACTION = 0.01  # ~1% of bytes edited per version, scattered


def _mutate(data: bytes, edits: int, seed: int) -> bytes:
    rng = random.Random(seed)
    out = bytearray(data)
    for _ in range(edits):
        out[rng.randrange(len(out))] = rng.randrange(256)
    return bytes(out)


def run(chunk_policy: bool) -> Dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="ancestree-bench-"))
    try:
        store = LineageStore(root / "store", chunk=chunk_policy)
        payload = random.Random(0).randbytes(SIZE_BYTES)

        started = time.perf_counter()
        for version in range(VERSIONS):
            payload = _mutate(
                payload, int(SIZE_BYTES * EDIT_FRACTION), seed=version + 1
            )
            with store.create_node(step_type="version") as node:
                (node / "data.bin").write_bytes(payload)
                node.add_meta("v", version)
        ingest_seconds = time.perf_counter() - started

        started = time.perf_counter()
        total_read = 0
        for record in store.find():
            total_read += len((record / "data.bin").read_bytes())
        read_seconds = time.perf_counter() - started

        stats = store.stats()
        store.close()
        return {
            "policy": "layer 1+2" if chunk_policy else "layer 1 only",
            "ingest_s": ingest_seconds,
            "read_s": read_seconds,
            "logical": int(stats["logical_bytes"]),
            "stored": int(stats["chunk_stored_bytes"]),
            "ratio": stats["dedup_ratio"],
            "read_bytes": total_read,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    print(
        f"{VERSIONS} versions of a {SIZE_BYTES // 1024} KiB artifact, "
        f"{EDIT_FRACTION:.0%} scattered in-place edits per version\n"
    )
    header = (
        f"{'policy':<14} {'ingest_s':>9} {'read_s':>8} "
        f"{'stored_KiB':>11} {'dedup_ratio':>12}"
    )
    print(header)
    print("-" * len(header))
    results = [run(False), run(True)]
    for result in results:
        print(
            f"{result['policy']:<14} {result['ingest_s']:>9.3f} "
            f"{result['read_s']:>8.3f} {result['stored'] // 1024:>11} "
            f"{result['ratio']:>12}"
        )
    layer1, layer2 = results
    print(
        f"\nLayer 2 stored {layer1['stored'] / max(layer2['stored'], 1):.1f}x "
        f"less than Layer 1 alone; ingest "
        f"{layer2['ingest_s'] / max(layer1['ingest_s'], 1e-9):.2f}x, read "
        f"{layer2['read_s'] / max(layer1['read_s'], 1e-9):.2f}x the time."
    )


if __name__ == "__main__":
    main()
