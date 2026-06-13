# Caveats & Limitations

Ancestree is deliberately small: a store is just a directory of nodes, and the search index is a cache layered over them. That design buys simplicity and crash-safety, but it also has sharp edges worth knowing before you lean on it. This page collects the behaviours that surprise people.

## Rules & configuration

Rules and generation triggers are written to `.lineage_config.json` the first time a store is created, and read back on every subsequent open. **They cannot be changed afterwards.** Passing different `rules` or `gen_triggers` to an existing store is gives a warning if the rules or gen_triggers differ from the ones on disk. The values on disk then win. To change them, start a new store or edit the config file directly.

Rules are also only as strict as you make them. A `step_type` that does not appear in `rules` has *no* transition constraint and can be created under any parent; rules only restrict the step types you actually list. A store created with no rules permits every transition.

## Nodes can vanish by design

`create_node` only persists a node if you write at least one artifact or add your own metadata inside the block. An untouched node is deleted when the context manager exits, with a `UserWarning` rather than an exception.

!!! warning
    The structural keys the store writes for you (`node_id`, `timestamp`, provenance, and so on) do not count as "touched". Overwriting only those still leaves the node empty, so it is still discarded.

If your code raises inside the `create_node` block, any partial work is kept and the node is flagged `healthy=False` before the exception re-raises. A node existing on disk therefore does not mean its block ran to completion — check the `healthy` flag, which is searchable via `find_node(healthy=False)`.

## The web graph

`generate_web_graph()` scans the node directories directly rather than the index, reads each `meta.json`, and writes a single self-contained `interactive_pipeline.html` at the store root, **overwriting any existing one**. Nodes whose `meta.json` cannot be read are skipped but print a warning. Because everything is inlined into one file, very large stores produce very large HTML.

## Concurrency

A single `LineageStore` instance is **not thread-safe**. The in-memory index is a plain dict mutated without locks, so sharing one instance across threads will corrupt it. Give each thread its own instance pointed at the same root instead.

Sequential multi-session use is safe. You can open a store, do some work, let the process exit, and reopen it later in a fresh process — the configuration and index persist, and the index is reconciled against the directories on load.

Parallel multiprocessing writes are safe *except during compaction*. Each process appends to the journal independently, but when one process folds the journal into the snapshot while another is mid-write, the concurrent writer's just-appended entry can be dropped from the snapshot.

!!! warning
    No node is ever lost this way — it is recovered from its `meta.json` on the next load — but the index can under-report until that reconciliation happens. If you fan writes out across processes, do it against the directories (which are the source of truth) and treat the index as eventually consistent.

## NFS

Ancestree is safe on **NFSv4** for normal use: it relies on atomic rename and append semantics, which NFSv4 honours, and the directories remain the source of truth regardless.

!!! warning
    Avoid triggering compaction from multiple processes simultaneously on NFS. NFS close-to-open consistency widens the compaction race described above, making a stale index more likely. Let one process own compaction, or compact only when no other process is writing.

## Index behaviour

The index lives in two files at the store root: `.index.json` (a compacted snapshot) and `.index.log` (an append-only journal of changes since the last snapshot). **Together** they are the index. `.index.json` on its own is not complete until compaction fires, so a new store, or one with only a handful of nodes, keeps its recent nodes only in the journal. Don't read `.index.json` directly and assume it is the full picture — the library always reads both and replays one over the other.

Because the directories are authoritative, a damaged or stale index is always recoverable. `store.rebuild_db_from_disk()` rescans every `meta.json` and rebuilds the index from scratch.

!!! warning
    A corrupt `.index.json` raises a `RuntimeError` on load with a message directing you to `store.rebuild_db_from_disk()` — corruption is loud rather than hidden. A single corrupt `meta.json` on an already-indexed node degrades gracefully instead: searches still answer from the index, but `get_node()` returns `None` for that node.

## Scale

Ancestree is built for **hundreds to low thousands of nodes**, not as a replacement for a proper database. Every search is a linear scan of the in-memory index, opening a store loads and reconciles the whole index, and `generate_web_graph()` reads every node's `meta.json` and inlines everything into one self-contained HTML file. None of this is a problem at the intended scale; all of it degrades linearly beyond it.

Compaction only fires once the journal has grown to roughly the snapshot's size, with a floor of 128 entries. A workflow that writes a single node per session will never reach that threshold, so the journal grows unbounded across sessions and store-open time creeps up as the whole journal is replayed each time.

!!! note
    If you write very few nodes per session over many sessions, call `store.rebuild_db_from_disk()` periodically. It rewrites a clean snapshot from the directories and clears the journal.

## Metadata coercion and overwrites

A handful of keys are reserved by the store — `parent_id`, `step_type`, `generation`, `timestamp`, `healthy`, `duration_s`, and `size_mb`. `add_meta` raises `ValueError` if you try to set one, so you cannot accidentally overwrite the structural metadata the store depends on for lineage, recency, and health.

`table` and `json` entries are always stored non-searchable, and `image`/`link` entries that point at files (rather than URLs) are rewritten relative to the store root and also forced non-searchable. They render in the web graph but cannot be matched by `find_node`.

The default `auto` data type infers the rendering from the value's *type*, not by sniffing string contents: a `Path` becomes an image (by file suffix) or a file link, a `dict`/`list` becomes JSON, a DataFrame becomes a table, and an `http(s)://` string becomes a link. Any other string stays plain `text` — a string that merely looks like a file path or filename is not treated as one. Pass `data_type` explicitly to override.

## Paths and artifacts

`artifacts(contains=...)` matches both as a glob and as a case-insensitive substring anywhere in the filename, and always excludes `meta.json`.

## Automatic provenance

Every node silently records who and what produced it: the OS user, Python version, platform, and the current git commit, branch, and dirty state. The git fields are captured by shelling out to `git`, which means a few subprocesses per node, and means your identity and repository state are recorded by default. Provenance entries are display-only (not searchable). Outside a git repository, or without git installed, the git fields are simply `None`.

## Search semantics

A predicate passed to `find_node` receives `None` for any key the node lacks, so a blanket-true predicate such as `lambda v: True` matches *every* node, including ones missing that key. A predicate that raises is treated as "no match" but provides a warning to the user that an error was raised.

`get_most_recent_node` ranks by the stored ISO timestamp. Nodes created in the same instant tie, and which one is returned is arbitrary. ISO timestamps give microsecond precision, so this is highly unlikely but worth noting as an extremely rare edge case.