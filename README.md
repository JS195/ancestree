# Ancestree

[![PyPI version](https://img.shields.io/pypi/v/ancestree-track?cacheSeconds=300)](https://pypi.org/project/ancestree-track/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/JS195/ancestree/blob/main/LICENSE)
[![Docs](https://github.com/JS195/ancestree/actions/workflows/deploy.yml/badge.svg)](https://github.com/JS195/ancestree/actions)
[![CI](https://github.com/JS195/ancestree/actions/workflows/ci.yml/badge.svg)](https://github.com/JS195/ancestree/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/JS195/ancestree/graph/badge.svg)](https://codecov.io/gh/JS195/ancestree)

**Exploratory pipeline tracking that sits in the gap between a messy folder-naming convention and a heavy lineage platform. Designed to fit around your workflow, not the other way round.**

![Pipeline Explorer](https://raw.githubusercontent.com/JS195/ancestree/main/docs/assets/preview.png)

No server required, no database to run, no dependencies. `pip install` + a local directory. The entire store is one SQLite file, driven purely from the Python standard library. For any workflow, not just machine learning.

---

## Contents

- [Why Ancestree?](#why-ancestree)
- [What makes it different](#what-makes-it-different)
- [Installation](#installation)
- [Quick start](#quick-start)
- [What's recorded automatically](#whats-recorded-automatically)
- [Searching and Querying](#searching-and-querying)
- [The Explorers](#the-explorers)
- [Coming from 0.1.x](#coming-from-01x)
- [Development](#development)
- [License](#license)

---

## Why Ancestree?

Iterative workflows are messy. You run ten variations, tweak parameters, rerun branches, and two weeks later you're staring at `final_v2_REAL.csv` wondering which preprocessing produced it and whether the code that made it was even committed. As your project grows, and you want to explore many ideas in parallel, or branch off a promising result and try multiple different things with it, a 'good folder naming convention' isn't going to cut it.

This is a problem in machine learning, but it's just as common in simulation, optimisation, data engineering, and document processing; any workflow where steps build on each other and results branch. Tools like MLflow solve it well, but only if you're doing ML and willing to stand up a server, otherwise the options are thin.

Ancestree solves it by modelling the pipeline as a directed acyclic graph. **Every step of your pipeline is a node**: its artifacts, its metadata, and where it came from, all recorded in a single local SQLite database. Chain nodes together and you get a complete, queryable data lineage tree of your work — durable, deduplicated, and visual when you want it to be.

---

## What makes it different

**It enforces lineage — it doesn't just record it.**
Rules are optional, but if you declare them: `rules={"model": ["clean"]}`, illegal transitions raise at creation time. Every other tracker is a passive logbook. This is active grammar for your pipeline.

**The whole store is one SQLite file.**
Nodes are rows, not folders. Metadata, the lineage graph and the artifact bytes all live in `<root>/ancestree.db` — back it up with `store.backup(dest)` (or by copying the file once the store is closed), query it with real SQL (`store.sql(...)` gives you a read-only escape hatch over a documented schema), and pull grep-able `meta.json` sidecars out any time with `store.export()`. One caveat I'll state up front: SQLite and NFS don't mix — keep stores on local disk.

**It deduplicates twice.**
Rerun a step that produces identical content and you get the *same node back*, not a copy. Underneath that, artifacts are split into content-defined chunks stored once — and near-identical artifacts (a config tweaked, values re-encoded) are stored as small deltas against what's already there. On a 69 MB corpus of six file types across six revisions that's 2.6× less storage — and data that already arrives compressed (PNG, parquet, zip) is kept verbatim rather than pointlessly re-compressed. `store.stats()` shows you the ratio on your own data.

**Forensic crash semantics.**
A step that raises mid-run keeps its partial output, flagged `healthy=False` and searchable. A step that wrote nothing vanishes with a warning. And if a run gets hard-killed, the next store open adopts whatever it managed to write as an unhealthy node. Partial work is evidence, not garbage.

**Reproducibility honesty, zero config.**
Every node captures who ran it, on what platform, with what Python, at which git commit — with a dirty-worktree flag so you know when a result isn't reproducible.

**Two ways to look at it.**
`store.host_live_graph()` hosts a live explorer on localhost — search, node diffs and a sortable runs table, re-rendered from the store on every refresh. `store.generate_web_graph()` still writes a self-contained HTML snapshot you can email to a colleague or attach to a PR — view-only, opens from `file://`, no login, no link that expires.

**Not ML-shaped.**
Step types are your vocabulary — ETL, simulation, lab protocol, report generation, image processing. The "runs/experiments/models" ontology that ML tools impose isn't here.

---

## Installation

Requires Python 3.9+. No dependencies.

```bash
pip install ancestree-track
```

## Quick Start

Wrap your existing save calls inside a standard Python context manager.

```python
import ancestree

# Rules declare which step types may follow which — your pipeline's grammar.
store = ancestree.LineageStore(
    root="./my_project",
    rules={"clean": ["ingest"], "model": ["clean"]},
)

# Each step runs inside a context manager. Write files with the / operator,
# attach anything worth remembering with add_meta.
with store.create_node(step_type="ingest") as node:
    df = do_process()

    df.to_csv(node / "raw.csv")
    node.add_meta("rows", len(df))

# The searchable explorer, live on localhost:
store.host_live_graph()
# ...or a self-contained snapshot you can share as-is:
store.generate_web_graph()
```

---

## What's recorded automatically

Every node silently captures the operational and reproducibility context without a single line of configuration.

| Captured      | Why it matters                                                                 |
| ------------- | ------------------------------------------------------------------------------ |
| `parent_id`   | Where the step came from (a list — joins with several inputs are fine)          |
| `generation`  | Which generation the step belongs to; useful for iterative workflows            |
| `step_type`   | The pipeline step being performed                                               |
| `created_utc` | When the step ran                                                               |
| `duration_s`  | How long it took — find slow steps, watch the pipeline getting slower           |
| `size_bytes`  | Total size of the node's artifacts                                              |
| `healthy`     | Whether the step completed, or raised mid-run                                   |
| `provenance`  | User, Python version, platform, git commit/branch, and the dirty-worktree flag  |

---

## Searching and Querying

Your history is a proper lineage DAG backed by SQL, so you can ask it real questions — with plain Python and native lambdas, or with SQL itself.

```python
store.find(step_type="model")  # all model runs
store.find(accuracy=lambda a: a and a > 0.9)  # the good ones
store.latest(step_type="clean")  # resume where you left off
store.lineage(best_model)  # its full ancestry, oldest first
store.ancestors(best_model, step_type="clean")  # which cleaning produced it?
best_model.artifacts("*.bin")  # locate its files
store.prune(bad_branch)  # preview a deletion (dry-run first)
store.prune(bad_branch, dry_run=False)  # ...and reclaim the space
store.backup("./nightly")  # consistent copy, even while open

store.sql("SELECT step_type, count(*) FROM node GROUP BY 1")  # read-only SQL
store.stats()  # counts, sizes, dedup ratio
```

---

## The Explorers

**Live** — `store.host_live_graph()` or `python -m ancestree serve ./my_project`. The lineage graph laid out by generation and coloured by step type; a search bar that understands `field=value`, numeric filters like `accuracy>0.9` and free text; click-to-inspect metadata with inline images and tables; pin two nodes for an aligned diff; and a sortable runs table for the "pick the best run" decisions. Light and dark themes. From a notebook it works like a Dash app: the call returns immediately, opens your browser, and the server keeps running in the background until the store closes (re-running the cell replaces it); the CLI form blocks until Ctrl+C.

**Snapshot** — `store.generate_web_graph()` renders the store into one self-contained HTML file: the graph plus click-to-view metadata, deliberately view-only. Small images ride along inside the file; anything bigger is copied next to it so links keep working offline.

There's also a small CLI: `python -m ancestree serve|export|compact <root>`.

---

## Coming from 0.1.x

0.2.0 is a rebuild on SQLite and a clean break: 0.1.x file-based stores are not readable by it and there is no migration tool — if you have an old store, keep 0.1.x installed for it. The API was redesigned at the same time (`find_node`→`find`, `get_lineage`→`lineage`, `get_most_recent_node`→`latest`, and so on — the renames are mechanical). The full reasoning, every decision and the old→new table live in [REBUILD_BLUEPRINT.md](https://github.com/JS195/ancestree/blob/main/REBUILD_BLUEPRINT.md).

---

## Development

Issues and PRs welcome.

Have a feature request or found a bug? Open an issue or reach out directly at [78921007+JS195@users.noreply.github.com](mailto:78921007+JS195@users.noreply.github.com).

```bash
git clone https://github.com/JS195/ancestree.git
cd ancestree
pip install -e ".[dev]"
python -m pytest tests/
```

---

## License

[MIT](https://github.com/JS195/ancestree/blob/main/LICENSE) © Joshua Smith
