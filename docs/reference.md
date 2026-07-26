# API Reference

`LineageStore` is the public entry point, available from the top-level `ancestree` package. Everything below is a method on it, apart from the two node types at the end.

The store is grouped here by what you are trying to do rather than alphabetically.

| Section | What it covers |
| --- | --- |
| [Creating a store](#creating-a-store) | Opening a store and setting its rules and policy |
| [Recording work](#recording-work) | Writing nodes, artifacts and metadata |
| [Searching and querying](#searching-and-querying) | Finding nodes and walking lineage |
| [Visualisation](#visualisation) | The static snapshot and the live explorer |
| [Maintenance](#maintenance) | Pruning, compacting, backing up and exporting |
| [Introspection](#introspection) | Direct SQL and store-level statistics |
| [Working with nodes](#working-with-nodes) | The two node types you receive |

---

## Creating a store

A store is a directory holding one SQLite database. Rules, generation triggers and the `reuse_identical`/`delta` policy are written in at creation and read back on every later open, so reopening by path is enough. See [Caveats](caveats.md#rules-and-policy-are-set-once) for what cannot be changed afterwards.

::: ancestree.LineageStore
    handler: python
    options:
        show_root_heading: true
        heading_level: 3
        separate_signature: true
        merge_init_into_class: true
        members: []

---

## Recording work

The one context manager everything else depends on. On clean exit the node commits atomically; if the block raises, partial work is kept and flagged `healthy=False`; an untouched node is discarded with a warning.

::: ancestree.LineageStore.create_node
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

---

## Searching and querying

`find` and `latest` search the whole store; `lineage`, `ancestors`, `children` and `from_parent` move around the graph. Filters match structural attributes and searchable metadata in one namespace, and a callable is treated as a predicate.

::: ancestree.LineageStore.find
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.latest
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.get
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.lineage
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.ancestors
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.children
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.from_parent
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

---

## Visualisation

Two ways to look at a store. `export_graph` writes a self-contained, view-only file you can share; `serve_graph` serves the searchable explorer, with diffs and the runs table, on localhost.

::: ancestree.LineageStore.export_graph
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.serve_graph
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

---

## Maintenance

Deleting, reclaiming space, and getting data out. `prune` defaults to a dry run. `backup` is the safe way to copy a store that is open; see [Caveats](caveats.md#one-file-is-the-whole-store-but-a-live-store-is-three) for why copying `ancestree.db` by hand is not.

::: ancestree.LineageStore.prune
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.compact
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.backup
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.export
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.close
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

---

## Introspection

The escape hatches, for questions the API above does not cover.

::: ancestree.LineageStore.sql
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

::: ancestree.LineageStore.stats
    handler: python
    options:
        show_root_heading: true
        show_root_full_path: false
        heading_level: 3
        separate_signature: true

---

## Working with nodes

You never construct a node yourself. `LineageStore.create_node` yields a recording handle, which is the object you write artifacts and metadata through inside the `with` block. The store's search and lineage methods return immutable `Node` records for everything already persisted.

::: ancestree.Node
    handler: python
    options:
        show_root_heading: true
        heading_level: 3
        unwrap_annotated: false
        members:
            - metadata
            - provenance
            - artifacts
            - __truediv__

::: ancestree.domain.node.RecordingNode
    handler: python
    options:
        show_root_heading: true
        heading_level: 3
        unwrap_annotated: false
        members:
            - add_meta
            - artifacts
            - __truediv__
