# Examples

Explore how to use **Ancestree** for different scenarios. Each example is a runnable Jupyter notebook rendered directly in these docs — the same workflows as the original 0.1.x demonstrations, on the current API.

<div class="grid cards" markdown>

- :material-school: **[Basic Usage](examples/basic_usage.ipynb)**

    Create a store, build a small lineage, and visualise it — the best place to start.

- :material-robot: **[Machine Learning Pipeline](examples/ml_pipeline.ipynb)**

    Track a realistic multi-step ML workflow: the iris dataset fanned out across scalers, embeddings and clusterers, then queried after the fact.

- :material-file-tree: **[Branching Stress Test](examples/stress_branching.ipynb)**

    A branching pipeline with a hyperparameter sweep — dedup on re-runs, chunk sharing across near-identical branches, and the metadata coercion edge cases.

- :material-timer-outline: **[CDC Deep Dive](examples/cdc_deep_dive.ipynb)**

    What the chunked store costs and saves: save/load timings against a native-filesystem baseline, per file type and size, plus the read-cache lifecycle.

</div>
