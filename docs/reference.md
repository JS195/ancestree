# API Reference

Technical documentation for the **Ancestree** lineage system. The `LineageStore` is the public entry point and is accessible directly from the top-level `ancestree` package — searching, lineage traversal, and visualisation all happen through its methods.

## Core Orchestration
The `LineageStore` is the prime entry point for managing your pipeline.

::: ancestree.LineageStore
    handler: python
    options:
        show_root_heading: true
        heading_level: 3
        separate_signature: true

---

## Working with Nodes
You never construct a node yourself. `LineageStore.create_node` yields a **recording handle** — the object you write artifacts and metadata through inside the `with` block — and the store's search and lineage methods return immutable **`Node` records** for everything already persisted.

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
