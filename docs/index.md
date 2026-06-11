# Ancestree

A lightweight Python tool for tracking data lineage with advanced search and visualisation.

## Features
!!! abstract ":material-graph: Interactive Graphs"
    Generate standalone HTML visualisations of your data flow.

!!! abstract ":material-shield-check: Rule Enforcement"
    Define strict transition rules to prevent invalid pipeline states.

!!! abstract ":material-database-search: Metadata Querying"
    Locate nodes instantly using deep-search metadata filters.

---

## Installation
<!-- !!! example ":material-download: Quick Install" -->
Install **Ancestree** directly via pip to get started.
```bash
pip install ancestree
```

## Getting Started
The core workflow involves initiating a `LineageStore` and creating `Nodes`.

```python
import ancestree

# Initialise
store = ancestree.LineageStore(root="./my_store", rules={"process":[None]})

# Create
with store.create_node(step_type="process") as node:
    node.add_meta("rows", 100, type="number")
```

## Next Steps
- Walk through the [Examples](examples.md) to see complete pipelines.
- Browse the [API Reference](reference.md) for full details on every class.