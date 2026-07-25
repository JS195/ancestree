# API Reference

`LineageStore` is the public entry point, available from the top-level `ancestree` package. Searching, lineage traversal and visualisation all happen through its methods.

## Core orchestration

::: ancestree.LineageStore
    handler: python
    options:
        show_root_heading: true
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
