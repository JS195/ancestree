# Ancestree

Data lineage tracking for Python, built on the standard library. Track each step of a pipeline, enforce valid transitions, and explore the result as an interactive graph.

[:material-rocket-launch: Quick Start](#quick-start){ .md-button }
[:material-cursor-default-click: Live Demo](demo.md){ .md-button }
[:material-notebook-outline: Examples](examples.md){ .md-button }
[:material-code-braces: API Reference](reference.md){ .md-button }
{: .hero-buttons }

---

## Features

<div class="grid cards" markdown>

- :material-graph:{ .lg .middle } **Interactive graphs**

    ---

    One call renders the pipeline as a self-contained HTML file. Open it in any browser, share it as-is, click a node for its metadata and artifacts.

- :material-shield-check:{ .lg .middle } **Rule enforcement**

    ---

    Rules are optional, but once declared, invalid transitions raise immediately rather than being recorded after the fact.

- :material-database-search:{ .lg .middle } **Metadata does double duty**

    ---

    Metadata is searchable by value or predicate, and also decides how each entry renders in the explorer.

- :material-feather:{ .lg .middle } **No dependencies**

    ---

    Pure standard library. Nothing to pin, nothing to conflict with, runs wherever Python 3.9+ runs.

- :material-restore:{ .lg .middle } **Crash-safe by design**

    ---

    Nodes are created in a context manager. If the code fails, partial work is kept and flagged unhealthy; untouched nodes are discarded.

- :material-database-outline:{ .lg .middle } **One SQLite file**

    ---

    Metadata, lineage and deduplicated artifact bytes all sit in a single `ancestree.db`. No server, nothing to configure. Back it up by copying one file, or query it with `store.sql(...)`.

</div>

## Quick Start

```bash
pip install ancestree-track
```

## How it works

A `LineageStore` is a directory holding one SQLite database. Every node is a row, with its artifacts stored as deduplicated, content-addressed chunks alongside its metadata and lineage.

```
my_store/
├── ancestree.db                 # the entire store: nodes, metadata, artifacts
├── interactive_pipeline.html    # generated web graph (optional snapshot)
├── .scratch/                    # a node's files, only while its block runs
└── .cache/                      # artifacts reassembled for reading, per session
```

Only `ancestree.db` holds anything you cannot regenerate. The dotted directories are working space: `.scratch/` holds a node's files while its `with` block runs and empties when the node commits, `.cache/` holds artifacts reassembled for reading and clears when the session ends. Deleting either at rest costs nothing. While a store is open SQLite also keeps `ancestree.db-wal` and `-shm` beside the database, so read [Caveats](caveats.md#one-file-is-the-whole-store-but-a-live-store-is-three) before backing one up.

Every write is a transaction, so a node is committed whole or not at all. `prune()` deletes a branch and reclaims the space, `export()` writes `meta.json` sidecars, and `store.sql(...)` queries a documented schema directly.

## Track, search and visualise

=== ":material-source-branch: Track"

    ```python
    import ancestree

    # "ingest" starts a pipeline; "clean" may only follow "ingest"
    store = ancestree.LineageStore(
        root="./my_store",
        rules={"ingest": [None], "clean": ["ingest"]},
    )

    with store.create_node(step_type="ingest") as node:
        node.add_meta("source", "warehouse")
    ```

=== ":material-magnify: Search"

    ```python
    # Match metadata by value, or by predicate
    cleaned = store.find(step_type="clean")
    big = store.find(rows=lambda r: r and r > 1000)

    # Pick up where you left off
    latest = store.latest(step_type="clean")

    # Trace a node's full ancestry, oldest first
    history = store.lineage(latest)
    ```

=== ":material-chart-timeline-variant: Visualise"

    ```python
    store.generate_web_graph()
    # Graph generated at my_store/interactive_pipeline.html
    ```

    Open the file in any browser. No server required.

## Metadata does double duty

Metadata is both a search index and the instruction set for how a node displays in the web graph. Each entry appears under its `group` heading, and `data_type` controls how the value renders. It defaults to `auto`, which infers the type from the value, and can be overridden.

```python
with store.create_node(step_type="model", parent=parent) as node:
    fig.savefig(node / "confusion.png")

    node.add_meta("accuracy", 0.94, group="Metrics")  # searchable, shown as text

    node.add_meta(
        "confusion_matrix",
        node / "confusion.png",  # rendered inline as a figure
        data_type="auto",
        group="Figures",
    )

    node.add_meta(
        "notes",
        "rerun after fix",  # display-only, excluded from search
        searchable=False,
    )
```

Metadata is not needed to expose files: every artifact appears as a clickable link under the node's **Artifacts** heading. Use `data_type="image"` to display a figure inline.

## Next steps

- Work through the [Examples](examples.md), including a [machine learning workflow](examples/ml_pipeline.ipynb).
- See the [API Reference](reference.md) for `LineageStore` and `Node`.
