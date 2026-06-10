# Ancestree

A lightweight Python tool for tracking data lineage, with advanced searching and visualisation for exploratory research.

## Features
 - **Web Visualisation:** Built-in `generate_web_graph()` transforms your complex data links into an interactive D3/Vis.js map instantly.
 - **Hierarchical Lineage:** Track parent-child relationships across many generations of data.
 - **State Lineage:** Custom `rules` enforce lineage working as a 'second brain'.
 - **Artifact Discovery:** Crawls and indexes files in node directories with targetted searching capability.
 - **Smart Metadata:** Supports recursive dictionaries, pandas DataFrame snapshots, and images.
 - **Visual Debugging:** Generates standalone HTML graphs of your entire data evolution.
 - **Zero-Dependency:** Runs on any Python 3.9+ environment with no external installs.
 - **Crash-Safe Iteration:** Python context managers handle data creation, if processing fails, the node is automatically rolled back preventing 'ghost' nodes.
 
---

## Requirements
```Python 3.9+```

## Dependencies
**None**. This library is built exclusively using the **Python Standard Library**. It requires no external packages.

---

## Installation
<!-- !!! example ":material-download: Quick Install" -->
Install **Ancestree** directly via pip to get started.
```bash
pip install ancestree
```
---

## Getting Started
The core workflow involves initiating a `LineageStore` and creating `Nodes`.

```python
import ancestree

# Initialise
store = ancestree.LineageStore(root="./my_store", rules={"process":[None]})

# Create
with store.create_node(step_type="process") as node:
    # do something
```