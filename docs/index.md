# Ancestree

**Lightweight, zero-dependency data lineage for Python.** Track every step of your pipeline, enforce valid transitions, and explore the whole flow as an interactive graph — built entirely on the standard library.

[:material-rocket-launch: Quick Start](#quick-start){ .md-button }
[:material-cursor-default-click: Live Demo](demo.md){ .md-button }
[:material-notebook-outline: Examples](examples.md){ .md-button }
[:material-code-braces: API Reference](reference.md){ .md-button }
{: .hero-buttons }

---

## Why Ancestree?

<div class="grid cards" markdown>

- :material-graph:{ .lg .middle } **Interactive graphs**

    ---

    One call renders your entire pipeline as a self-contained HTML file — open it in any browser, share it as-is, click any node to inspect its metadata and artifacts.

- :material-shield-check:{ .lg .middle } **Rule enforcement**

    ---

    Rules are optional. But if you declare them, invalid transitions raise immediately so your pipeline can't drift into impossible states.

- :material-database-search:{ .lg .middle } **Metadata works twice**

    ---

    Metadata entries are searchable by exact value or predicate, whilst also used as instructions to render the entry in the explorer.

- :material-feather:{ .lg .middle } **Zero dependencies**

    ---

    Pure Python standard library. Nothing to pin, nothing to conflict with, it runs anywhere Python 3.9+ runs.

- :material-restore:{ .lg .middle } **Crash-safe by design**

    ---

    Nodes are created in a context manager. If your code fails, partial work is kept and flagged unhealthy; untouched nodes vanish without a trace.

- :material-database-outline:{ .lg .middle } **One SQLite file**

    ---

    The whole store — metadata, lineage and deduplicated artifact bytes — is a single `ancestree.db`. No server, no database to run, nothing to configure. Back it up by copying one file, or query it directly with `store.sql(...)`.

</div>

## Quick Start

Install **Ancestree** directly via pip:

```bash
pip install ancestree-track
```

## How it works

There is no hidden state: a `LineageStore` is a root directory holding one SQLite database, and every node is a row in it — its artifacts stored as deduplicated, content-addressed chunks alongside its metadata and lineage.

```
my_store/
├── ancestree.db                 # the entire store: nodes, metadata, artifacts
├── interactive_pipeline.html    # generated web graph (optional snapshot)
├── .scratch/                    # a node's files, only while its block runs
└── .cache/                      # artifacts reassembled for reading, per session
```

Only `ancestree.db` holds anything you cannot regenerate. The two dotted directories are working space: `.scratch/` holds a node's files while its `with` block runs and is emptied when the node commits, and `.cache/` holds artifacts reassembled for reading and is cleared when the session ends. Deleting either at rest costs you nothing. While a store is *open*, SQLite also keeps `ancestree.db-wal` and `-shm` beside the database — see [Caveats](caveats.md#one-file-is-the-whole-store--but-a-live-store-is-three) before you back one up.

Every write is a real transaction, so a node is either committed whole or not at all. Delete a branch with `prune()` — it reclaims the space for you; pull grep-able `meta.json` sidecars out any time with `export()`; or ask the database anything with `store.sql(...)` — the schema is documented and versioned.

## Track, search, and visualise your pipeline:

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

    Open the file in any browser — no server required.

## Metadata does double duty

Metadata isn't just a search index — it's also the instruction set for how each node is displayed in the web graph. Every entry you add appears in the node's panel, organised under its `group` heading, and its `data_type` controls how the value is rendered. `data_type` defaults to `auto` and the store will infer the correct data_type but this can be manually overridden offering flexibiliy. 

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

You don't need metadata to expose your files: every artifact a node contains automatically appears as a clickable link under its **Artifacts** heading. Use `data_type="image"` when you want a figure actually displayed inline — a confusion matrix, a loss curve, a sample plot — so the graph doubles as a visual report of your pipeline.

## Next steps

- Walk through the [Examples](examples.md) to see complete pipelines, including a full [machine learning workflow](examples/ml_pipeline.ipynb).
- Browse the [API Reference](reference.md) for full details on `LineageStore` and `Node`.
