# Examples

Runnable Jupyter notebooks, rendered directly in these docs.

<div class="grid cards" markdown>

- :material-school: **[Basic Usage](examples/basic_usage.ipynb)**

    Create a store, build a small lineage, visualise it. Start here.

- :material-robot: **[Machine Learning Pipeline](examples/ml_pipeline.ipynb)**

    A multi-step workflow: the iris dataset fanned out across scalers, embeddings and clusterers, then queried after the fact.

</div>

## Worked pipelines

Four longer examples from different fields. Each takes a different graph shape, so together they cover the whole API.

<div class="grid cards" markdown>

- :material-dna: **[Genomics Cohort](examples/genomics_cohort.ipynb)**

    A fan-in. Eight samples down identical paths, joined into one cohort. Rules, a sample that fails QC and is kept as evidence, and a multi-parent join.

- :material-chart-bell-curve: **[Design Optimisation](examples/design_optimisation.ipynb)**

    A chain. Simulated annealing over a heat sink, thirty designs deep. Deep lineage, an abandoned branch, and `prune` reclaiming its space.

- :material-telescope: **[Survey Reprocessing](examples/survey_reprocessing.ipynb)**

    A campaign. A telescope survey reduced twice after a calibration fault. Generations, identical-node reuse, and scoring two runs against each other.

- :material-finance: **[Quant Backtest Grid](examples/quant_backtest.ipynb)**

    A lattice. Strategy configurations across walk-forward folds, with a look-ahead bug planted, traced and withdrawn. Contamination tracing, `reuse_identical=False` for audit, and counting trials for a multiple-testing correction.

</div>
