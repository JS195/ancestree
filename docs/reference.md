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
You never construct a `Node` yourself — they are created by `LineageStore.create_node` and returned by the store's search and lineage methods. You will interact with them to read and attach metadata, locate artifacts, and build paths inside a node's directory.

::: ancestree.models.Node
    handler: python
    options:
        show_root_heading: true
        heading_level: 3
        unwrap_annotated: false
        members:
            - metadata
            - add_meta
            - artifacts
            - __truediv__
