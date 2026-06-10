# API Reference 

Technical documentation for the **Ancestree** lineage system. All core classes and utilities are accessible directly from the top-level `ancestree` package.

## Core Orchestration
The `LineageStore` is the prime entry point for managing your pipeline.

::: ancestree.LineageStore
    options:
        show_root_heading: true
        heading_level: 3
        separate_signature: true

---

## Data models
The `Node` class represents the actual directories and associated metadata stored on the disk.

::: ancestree.Node
    options:
        show_root_heading: true
        heading_level: 3

## Formatting Metadata
Metadata requires formatting for decoding and display in the interactive HTML.

::: ancestree.format_metadata
    options:
        show_root_heading: true
        heading_level: 3

<!-- ---

## Visualisation
Helper functions for generating graphs and formatting metadata.
::: ancestree.vis -->