## [0.1.0] — 2026-06-12

### Added
- `LineageStore` — core store for creating, searching, and managing pipeline nodes
- `Node` — represents a single pipeline step; holds artifacts and metadata on disk
- Rule enforcement — declare valid step-type transitions; invalid transitions raise immediately
- `gen_triggers` — declare which step types increment the generation counter
- Automatic provenance capture — user, platform, Python version, git commit, branch, and dirty-worktree flag recorded on every node
- Automatic timing and size capture — `duration_s` and `size_mb` recorded on every node
- Crash-safe context manager — failed nodes flagged `healthy=False`; empty nodes removed silently with a warning
- `add_meta()` — attach searchable, typed metadata to any node; supports `text`, `number`, `image`, `link`, `table`, `json`, and `code` types with auto-inference
- `get_node()` — resolve a node_id string into a Node object
- `find_node()` — search the store by metadata value or predicate
- `find_in_lineage()` — search within a node's ancestry
- `get_lineage()` — return a node's full ancestry, oldest first
- `get_most_recent_node()` — return the most recently created node matching a query
- `get_child_nodes()` — return direct descendants of a node
- `from_parent()` — shortcut to read artifacts from a node's parent
- `artifacts()` — list files inside a node's directory, with glob and substring filtering
- `prune()` — delete a node and its descendants, with dry-run support
- `rebuild_db_from_disk()` — recover and resync the search index from disk
- `generate_web_graph()` — render the entire store as a self-contained interactive HTML file
- Pipeline Explorer — lineage graph, metadata search, health indicators, colour-by-metric heatmap, runs table, activity timeline, node compare, and inline image/table rendering
- `lineage_database` — mtime-based, process-safe in-memory index with atomic snapshot replacement
- Zero dependencies — pure Python standard library throughout
- Full Google-style docstrings and MkDocs Material documentation site
- Jupyter notebook examples: basic usage, ML pipeline, 10k-node timing benchmark