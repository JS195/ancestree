# Ancestree

[![PyPI version](https://img.shields.io/pypi/v/ancestree)](https://pypi.org/project/ancestree/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Docs](https://github.com/JS195/ancestree/actions/workflows/deploy.yml/badge.svg)](https://github.com/JS195/ancestree/actions)

**A second brain for your data pipelines.** Track the full ancestry of every artifact you produce — who made it, from what, how long it took, and whether you can trust it — then explore the whole story in an interactive map.

No database server. No cloud infrastructure. No daemon. No dependencies. Pure Python standard library on top of your filesystem. Works with plain Python scripts and Jupyter notebooks. Instantiate only when you need it, nothing runs in the background.

Designed to fit seamlessly around your code. One context manager and one method (`add_meta`) covers the core usage. No need to learn new syntax, saving files remains the same calls you already write.

---

## Contents

- [Why Ancestree?](#why-ancestree)
- [Installation](#installation)
- [Quick start](#quick-start)
- [What's recorded automatically](#whats-recorded-automatically)
- [Searching and Querying](#searching-and-querying)
- [The Pipeline Explorer](#the-pipeline-explorer)
- [Design principles](#design-principles)
- [Documentation & examples](#documentation--examples)
- [Development](#development)
- [License](#license)

---

## Why Ancestree?

Exploratory research is messy. You clean a dataset three different ways, train ten models, overwrite half your outputs, and two weeks later you're staring at `final_v2_REAL.csv` wondering which preprocessing produced it — and whether the code that made it was even committed.

Ancestree solves this with one idea: **every step of your pipeline is a node**. A node is just a directory that holds the step's outputs plus a metadata record describing where it came from. Chain nodes together and you get a complete, queryable family tree of your data — durable on disk, reconstructable at any time, and visual when you want it to be.

---

## Installation

Requires Python 3.9+. No dependencies.

```bash
pip install ancestree
```

## Quick Start

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

# One call: a self-contained, interactive map of everything above.
store.generate_web_graph()
```

---

## What's recorded automatically

Every node automatically records the things you'll wish you had written down:


| Captured     | Why it matters                                                                                                                |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `parent_id`  | Where did this step come from?                                                                                                |
| `generation`  | What generation is this? Useful for iterative workflows if numerous steps happen in one generation.                          |
| `step_type`  | What step is this performing?                                                                                                 |
| `timestamp`  | When the step ran                                                                                                             |
| `duration_s` | How long it took — find the slow step, see the pipeline getting slower                                                        |
| `size_mb`    | Total size of the node's artifacts                                                                                            |
| `healthy`    | Whether the step completed, or raised mid-run                                                                                 |
| `provenance`   | User, Python version, platform, git commit/branch, and a **dirty-worktree flag** so you know when a result isn't reproducible |


### Crash-safe by design

If your code raises inside `create_node`, anything already written is kept and the node is flagged `healthy=False` — partial work is evidence, not garbage. If the step wrote nothing at all, the node silently vanishes: no ghost directories, ever.

---

## Searching and Querying

The store indexes every searchable metadata key, so questions become one-liners:

```python
store.find_node(step_type="model")                          # all model runs
store.find_node(accuracy=lambda a: a and a > 0.9)           # the good ones
store.get_most_recent_node(step_type="clean")               # resume where you left off
store.get_lineage(best_model)                               # its full ancestry, oldest first
store.find_in_lineage(best_model, step_type="clean")        # which cleaning produced it?
best_model.artifacts("*.bin")                                # locate its files
store.prune(bad_branch)                                      # preview a deletion (dry-run first)
```

---

## The Pipeline Explorer

![Pipeline Explorer](docs/assets/preview.png)

`generate_web_graph()` renders the entire store into **one self-contained HTML file** — every style and script inlined, so it opens anywhere and ships as-is to a colleague or a static site.

Inside it:

- **Lineage graph** — nodes laid out by generation and coloured by step type; hovering a node lights up its complete ancestry and descendants.
- **Search that understands your metadata** — free text, `field=value`, and numeric operators like `accuracy>0.9` allow easy navigation.
- **Compare** — cmd-click two nodes for an aligned diff: identical values recede, differences are highlighted with numeric deltas.
- **Rich metadata** — inline images, file links, and pandas DataFrames rendered as tables (`data_type="table"`), grouped into sections you define. Light and dark themes included.
- **Runs table** — flip the graph into a sortable table of runs × metrics: the "pick the best run" view when decisions trade accuracy against runtime against data size.


---

## Documentation & examples

- **[Documentation site](https://js195.github.io/ancestree/)** — full API reference and a live demo of the explorer.
- **Example notebooks** in [docs/examples/](docs/examples/): [basic usage](docs/examples/basic_usage.ipynb), an end-to-end [ML pipeline](docs/examples/ML_pipeline.ipynb), and a [10k node timing benchmark](docs/examples/timing_benchmark.ipynb).

---

## Development
Issues and PRs welcome.

```bash
git clone https://github.com/JS195/ancestree.git
cd ancestree
pip install -e .
python -m pytest tests/
```

Have a feature request or found a bug? Open an issue or reach out directly at [josh.smith195@outlook.com](mailto:josh.smith195@outlook.com).

---

## License

[MIT](LICENSE) © Joshua Smith