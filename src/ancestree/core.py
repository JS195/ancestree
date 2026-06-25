# Python packages
from pathlib import Path
import json
import os
import time
import uuid
from typing import List, Dict, Any, Optional, Union, Iterator
import shutil
from contextlib import contextmanager
import warnings

# Internal dependancies
from .chunkstore import ChunkStore, drop_read_cache
from .database import lineage_database
from .models import ARTIFACT_MANIFEST, Node
from .vis import run_web_generator


class LineageStore:
    """
    Orchestrates the lineage and interactions of a data pipeline.

    The LineageStore manages the physical storage, rule enforcement, and hierarchical relationships between different steps in a data pipeline.
    The rules need only be specified once as configurations persist. The LineageStore does not need to exist in memory. It can be recreated any time it is required.
    Provides advanced searching capabilities across the node network.
    """

    def __init__(
        self,
        root: Union[Path, str],
        rules: Optional[Dict[str, Any]] = None,
        gen_triggers: Optional[List[str]] = None,
        dedupe: bool = True,
        chunk: bool = True,
    ):
        """
        Initialises the LineageStore, ensures its directory exists, and loads or creates the ruleset configuration.

        On creation the LineageStore saves a .lineage_config.json file. On subsequent re-creation, the store reads from this file. There is no need to resupply rules or gen_triggers at any point after initial creation even if the store no longer exists in memory. The rules and gen_triggers cannot be changed after initial creation.

        Args:
            root (Union[Path, str]): Root directory for data pipeline. This is where the nodes sit.
            rules (Dict, optional): A mapping defining the allowed transitions. Defaults to None.
            gen_triggers (List, optional): Step types that mark a new generation. When a node of this type is created, its generation number increments by one relative to its parent. Defaults to None.
            dedupe (bool, optional): When True, a node that is content-identical to one already in the store is not created a second time: `create_node` reuses the existing node instead, and the variable yielded by the `with` block points at it. Two nodes are content-identical when they share the same step_type, the same parent, the same user metadata, and byte-identical artifacts; volatile fields (node_id, timestamp, duration, size) and provenance (user, platform, git state) are ignored. A candidate match is byte-verified before reuse. This is a behaviour of the store instance and is not persisted in the config — pass it each time you open a store you want to deduplicate. Defaults to False.
            chunk (bool, optional): When True, artifacts are deduplicated at sub-file granularity. As each node is persisted, its files are split into content-defined chunks stored once in a shared pool (`<root>/.chunks`); near-identical artifacts across nodes then cost only their differing chunks. This is transparent: you write files normally inside `create_node`, and reading them back (`node / "file"`, `node.artifacts()`, the web graph) reassembles them on demand. Space is reclaimed by calling `compact()`. Like `dedupe`, this is a per-instance behaviour and is not persisted in the config. Defaults to False.

        Examples:
            >>> rules = {"clean": ["ingest"], "model": ["clean"]}
            >>> store = LineageStore("my_project", rules=rules, gen_triggers=["ingest"])
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        self.config_path = self.root / ".lineage_config.json"
        config = self._do_config(rules, gen_triggers)
        self.rules = config["rules"]
        self.triggers = config["triggers"]
        self.dedupe = dedupe
        self.chunk = chunk
        self.database = lineage_database(self.root)

    def __enter__(self) -> "LineageStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        # Wipe the read cache deterministically at the end of a `with` block.
        # (It is also wiped at interpreter exit, so non-context-manager use is
        # cleaned up too.)
        self.clear_cache()

    def clear_cache(self) -> None:
        """
        Discards the store's session read cache (`<root>/.cache`).

        Reading a packed artifact reassembles its bytes into this cache rather
        than back into the node directory, so the store never needs `compact()`
        to reclaim what reads would otherwise leak. The cache is pure derived
        data — anything in it is regenerated from the chunk pool on the next
        read — so clearing it only frees disk, never loses anything. It is also
        cleared automatically when the process exits or a `with` block closes;
        call this to reclaim the space sooner.
        """
        drop_read_cache(self.root)

    def _do_config(
        self,
        supplied_rules: Optional[Dict[str, Any]],
        supplied_triggers: Optional[List[str]],
    ) -> Dict[str, Any]:
        is_new = not self.config_path.exists()

        if is_new:
            tmp = self.root / f".lineage_config.{uuid.uuid4().hex}.tmp"
            try:
                tmp.write_text(
                    json.dumps(
                        {"rules": supplied_rules, "triggers": supplied_triggers},
                        indent=2,
                    )
                )
                tmp.replace(self.config_path)
            finally:
                tmp.unlink(missing_ok=True)

        config = json.loads(self.config_path.read_text())

        if not is_new:
            stored_rules = config.get("rules") or {}
            stored_triggers = config.get("triggers") or []
            if supplied_rules and supplied_rules != stored_rules:
                warnings.warn(
                    "Supplied rules differ from the stored configuration and have been ignored. "
                    "Rules cannot be changed after a store is created.",
                    UserWarning,
                    stacklevel=3,
                )
            if supplied_triggers and supplied_triggers != stored_triggers:
                warnings.warn(
                    "Supplied gen_triggers differ from the stored configuration and have been ignored. "
                    "gen_triggers cannot be changed after a store is created.",
                    UserWarning,
                    stacklevel=3,
                )

        return {
            "rules": config.get("rules") or {},
            "triggers": config.get("triggers") or [],
        }

    # ---------------------------------------------------------------------------
    # Node creation
    # ---------------------------------------------------------------------------

    @contextmanager
    def create_node(
        self, step_type: str, parent: Union["Node", str, None] = None
    ) -> Iterator["Node"]:
        """Creates a new node while enforcing lineage rules.

        The node only materialises on disk once the user writes an artifact or
        adds metadata; an untouched node is discarded with a warning. If the
        user's code raises after writing, the partial work is persisted and the
        node's 'healthy' metadata flag is set to False (True on clean completion).

        If the store was opened with `dedupe=True` and the block completes
        cleanly, a node that is content-identical to one already in the store
        is not created again: the existing node is reused and the `as node`
        variable is rebound onto it. See `LineageStore.__init__` for what counts
        as content-identical.

        Args:
            step_type (str): The type of pipeline step being performed.
            parent (Union[Node, str], optional): The parent Node object or node_id. Defaults to None.

        Raises:
            ValueError: If the step type transition is not permitted according to the store rules.

        Yields:
            Node: The new node, ready to receive artifacts and metadata.

        Examples:
            >>> with store.create_node(step_type="clean", parent=ingest_node) as node:
            ...     df.to_csv(node / "cleaned.csv")
            ...     node.add_meta("rows", len(df))
        """

        parent_node = self.get_node(parent)
        parent_type = parent_node.step_type if parent_node else None

        # Check to ensure not illegal node creation
        allowed = self.rules.get(step_type)
        if allowed is not None and parent_type not in allowed:
            raise ValueError(
                f"Invalid transition: {parent_type} -> {step_type}. "
                f"Allowed parents: {allowed}."
            )

        parent_gen = parent_node.generation if parent_node else 0
        if parent_node and (step_type in self.triggers):
            current_gen = parent_gen + 1
        else:
            current_gen = parent_gen

        node_id = uuid.uuid4().hex[:8]
        while node_id in self.database.cache or (self.root / node_id).exists():
            node_id = uuid.uuid4().hex[:8]
        node_path = self.root / node_id

        parent_id = parent_node.node_id if parent_node else None
        new_node = Node._create(
            node_path, node_id, current_gen, parent_id, step_type=step_type
        )

        start = time.monotonic()
        try:
            yield new_node
        except BaseException:
            # Keep partial work: anything written before the failure persists,
            # flagged as unhealthy. An untouched node leaves no trace.
            if not self._persist_if_touched(
                new_node, healthy=False, duration=time.monotonic() - start
            ):
                shutil.rmtree(new_node.path, ignore_errors=True)
            raise

        if not self._persist_if_touched(
            new_node, healthy=True, duration=time.monotonic() - start
        ):
            shutil.rmtree(new_node.path, ignore_errors=True)

            warnings.warn(
                f"Node '{new_node.node_id}' (step_type='{step_type}') was discarded: "
                "no artifacts were written and no metadata was added. "
                "Write at least one file or call node.add_meta() to persist the node.",
                UserWarning,
                # 1=here, 2=contextlib.__exit__ (next(self.gen)), 3=user `with`.
                stacklevel=3,
            )

    def _persist_if_touched(self, node: "Node", healthy: bool, duration: float) -> bool:
        """
        Persists and indexes the node if the user wrote any artifact or
        metadata, recording whether its code block ran to completion in the
        'healthy' flag, how long the block took in 'duration_s', and the
        total size of its files in 'size_mb'. Returns True if the node was
        persisted.

        When the store has dedupe enabled and the node completed cleanly, a
        node that is content-identical to an existing one is not persisted
        again: the existing node is reused (see `_deduplicate`) and True is
        returned without writing a second copy.
        """
        has_artifacts = bool(node.artifacts())
        has_user_meta = bool(set(node._hydrate()) - node._system_keys)
        if not (has_artifacts or has_user_meta):
            return False
        # Only deduplicate clean completions: a failed (unhealthy) run holds
        # partial work that should never be merged into a healthy node.
        if self.dedupe and healthy and self._deduplicate(node):
            return True
        size = sum(f.stat().st_size for f in node.path.rglob("*") if f.is_file())
        node._set_meta(
            "healthy", healthy, data_type="text", group="Structural Properties"
        )
        node._set_meta(
            "duration_s",
            round(duration, 3),
            data_type="text",
            group="Structural Properties",
        )
        node._set_meta(
            "size_mb",
            round(size / 1e6, 6),
            data_type="text",
            group="Structural Properties",
        )
        node._write_meta()
        self.database.add(node.node_id, node.to_db())
        # Sub-file deduplication: chunk the artifacts into the shared pool once
        # the node is safely persisted and indexed. Only clean completions are
        # packed; a failed run's partial files are left untouched on disk.
        if self.chunk and healthy:
            node._pack()
        return True

    def _deduplicate(self, node: "Node") -> bool:
        """
        If `node` is content-identical to a node already in the store, rebinds
        it onto that existing node, discards the directory just written, and
        returns True. Otherwise stamps the node with its content hash so future
        runs can find it, and returns False.

        The content hash is only a fast bucket key: a candidate it points to is
        byte-verified with `_content_equal` before reuse, so a hash collision
        can never merge two genuinely different nodes.
        """
        content_hash = node.content_hash()
        candidate_id = self.database.find_by_hash(content_hash)
        if candidate_id:
            candidate = self.get_node(candidate_id)
            if candidate and node._content_equal(candidate):
                self._adopt(node, candidate)
                return True
        # New content (or an astronomically unlikely hash collision): record
        # the hash so a later identical node deduplicates against this one.
        node._set_meta(
            "content_hash",
            content_hash,
            data_type="text",
            group="Structural Properties",
        )
        return False

    @staticmethod
    def _adopt(node: "Node", existing: "Node") -> None:
        """
        Rebinds `node` in place onto an existing, content-identical node and
        deletes the directory `node` had just written. Because `create_node`
        yields this same object to the user, their `with ... as node` variable
        transparently becomes the existing node — including when later passed
        as a `parent`.
        """
        stale_dir = node.path
        node.node_id = existing.node_id
        node.path = existing.path
        node.generation = existing.generation
        node.parent_id = existing.parent_id
        node.step_type = existing.step_type
        node._metadata = existing._hydrate()
        node._system_keys = set(existing._hydrate())
        if stale_dir.resolve() != existing.path.resolve():
            shutil.rmtree(stale_dir, ignore_errors=True)

    # ---------------------------------------------------------------------------
    # Searching and Querying
    # ---------------------------------------------------------------------------

    def get_node(self, node: Union[str, "Node", None] = None) -> Optional["Node"]:
        """
        Resolves a node_id string into a Node object, loading it from disk.

        Accepts a Node as well (returned unchanged), so it can be used to normalise any "node or id" argument. Returns None rather than raising if the node does not exist or its metadata cannot be read.

        Args:
            node (Union[str, Node, None]): A node_id string, an existing Node instance, or None.

        Returns:
            Optional[Node]: The resolved Node, or None if the input is None, invalid, or not found.

        Examples:
            >>> node = store.get_node("abc12345")
        """
        if not node or str(node).lower() == "none":
            return None
        if isinstance(node, Node):
            return node
        node_path = self.root / node
        if not node_path.exists():
            return None
        try:
            return Node._load(node_path)
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            return None

    def get_most_recent_node(self, **kwargs: Any) -> Optional["Node"]:
        """
        Finds the single most recently created node that matches the given search parameters.

        Recency is determined by the timestamp recorded when each node was created. Useful for picking up a pipeline where it left off, e.g. fetching the latest cleaned dataset.

        Args:
            **kwargs (Any): Key-value pairs to match against the nodes' searchable metadata keys, as in `find_node`.

        Returns:
            Optional[Node]: The most recent matching node, or None if nothing matches.

        Examples:
            >>> latest = store.get_most_recent_node(step_type="clean")
        """
        # find_matches is called here (not nested inside a database helper) so
        # a raising predicate warns at the same stacklevel as find_node.
        matches = self.database.find_matches(**kwargs)
        str_id = self.database.most_recent(matches)
        return self._node_from_index(str_id) if str_id else None

    def from_parent(self, node: Union[str, "Node"], filename: str) -> List[Path]:
        """
        Shortcut to get specific file(s) from the parent node of the specified node.

        Equivalent to looking up the node's parent and calling `artifacts` on it. The typical use is reading the previous step's output as the current step's input.

        Args:
            node (Union[str, Node]): The Node or node_id whose parent to search.
            filename (str): A glob pattern or substring to match against the parent's files, as in `Node.artifacts`.

        Returns:
            List[Path]: The matching file paths from the parent node, ready to read directly (as returned by `Node.artifacts`). Empty if the node or its parent cannot be found, the node has no parent, or nothing matches.

        Examples:
            >>> with store.create_node(step_type="model", parent=clean_node) as node:
            ...     [training_data] = store.from_parent(node, "cleaned.csv")
        """
        resolved = self.get_node(node)
        if resolved is None or resolved.parent_id is None:
            return []
        parent_node = self.get_node(resolved.parent_id)
        return parent_node.artifacts(filename) if parent_node else []

    def find_node(self, **kwargs: Any) -> List["Node"]:
        """
        Search for nodes based on metadata key values. Values are matched by
        equality against the searchable metadata; pass a callable to express
        a predicate instead.

        Args:
            **kwargs (Any): Key-value pairs to match against the node's searchable metadata keys.

        Returns:
            List['Node']: A list of node objects that match all provided criteria.

        Examples:
            >>> store.find_node(step_type="ingest")
            >>> store.find_node(accuracy=lambda a: a is not None and a > 0.8)
        """
        return [
            self._node_from_index(node_id)
            for node_id in self.database.find_matches(**kwargs)
        ]

    def get_lineage(self, node: Union[str, "Node"]) -> List["Node"]:
        """
        Traces the ancestry of the node.

        Args:
            node (str | Node): The Node or node_id to trace from.

        Returns:
            List['Node']: A list of Node objects ordered from oldest ancestor to the target node.

        Examples:
            >>> history = store.get_lineage("abc12345")
            >>> [n.step_type for n in history]
            ['ingest', 'clean', 'transform']
        """
        if isinstance(node, Node):
            node = node.node_id
        return [
            self._node_from_index(node_id)
            for node_id in self.database.get_lineage(node)
        ]

    def find_in_lineage(self, node: Union[str, "Node"], **kwargs: Any) -> List["Node"]:
        """
        Searches a node's ancestry for nodes matching specified search parameters.

        This is `find_node` restricted to a single lineage: only the target node and its ancestors are considered. Values are matched by equality against the searchable metadata; pass a callable to express a predicate instead.

        Args:
            node (Union[str, Node]): The Node or node_id whose ancestry to search.
            **kwargs (Any): Key-value pairs to match against the nodes' searchable metadata keys.

        Returns:
            List[Node]: The nodes in the lineage that match all provided criteria.

        Examples:
            >>> store.find_in_lineage(model_node, step_type="clean")
        """
        if isinstance(node, Node):
            node = node.node_id
        return [
            self._node_from_index(node_id)
            for node_id in self.database.find_in_lineage(node, **kwargs)
        ]

    def get_child_nodes(self, node: Union[str, "Node"]) -> List["Node"]:
        """
        Returns the direct children of the specified node.

        Only immediate offspring are returned, not the full subtree. To walk further down the branch, call this on each child in turn.

        Args:
            node (Union[str, Node]): A Node object or node_id string.

        Returns:
            List[Node]: All nodes whose parent is the specified node. Empty if the node has no children or does not exist.
        """
        target = self.get_node(node)
        return self.find_node(parent_id=target.node_id) if target else []

    def _node_from_index(self, node_id: str) -> "Node":
        """
        Builds a Node from the in-memory index without touching disk; the
        full metadata is hydrated lazily on first access. Only valid for ids
        present in the index.
        """
        return Node._from_index(self.root / node_id, self.database.cache[node_id])

    # ---------------------------------------------------------------------------
    # Maintenance
    # ---------------------------------------------------------------------------

    def rebuild_db_from_disk(self) -> None:
        """
        Rebuilds the search index by scanning all node directories on disk.

        Use this as a recovery step if the index becomes stale or corrupt —
        for example after a crash mid-write, manual filesystem changes, or a
        KeyError from get_lineage suggesting a missing index entry.

        Note: only nodes with a valid meta.json are re-indexed. Directories
        without one are silently skipped.
        """
        self.database.rebuild_from_disk()

    def prune(self, node: Union[str, "Node"], dry_run: bool = True) -> List["Node"]:
        """
        Deletes a node and all of its descendants, purging the entire branch.

        THIS IS RECURSIVE — deleting a node deletes anything downstream of it, removing both the directories on disk and their index entries. Run with the default `dry_run=True` first to preview exactly what would be removed.

        Args:
            node (Union[str, Node]): Either a node_id string or a Node object.
            dry_run (bool, optional): If True, returns the list of nodes that would be deleted without deleting anything. Must be set to False to actually delete. Defaults to True.

        Returns:
            List[Node]: Nodes that were (or would be) deleted, deepest first.

        Raises:
            PermissionError: If the target resolves to the store's root directory.

        Examples:
            >>> store.prune("abc12345")                  # preview only
            >>> store.prune("abc12345", dry_run=False)   # actually delete
        """
        deleted = self._prune(node, dry_run=dry_run)
        # Deleting nodes orphans the chunks only they referenced; reclaim them.
        if deleted and not dry_run and self.chunk:
            self.gc()
        return deleted

    def _prune(self, node: Union[str, "Node"], dry_run: bool) -> List["Node"]:
        target = self.get_node(node)
        if not target:
            return []

        if target.path.resolve() == self.root.resolve():
            raise PermissionError("Cannot prune the root lineageStore directory.")

        deleted = []
        for child in self.get_child_nodes(target):
            deleted.extend(self._prune(child, dry_run=dry_run))

        if not dry_run:
            shutil.rmtree(target.path)
            self.database.remove(target.node_id)

        deleted.append(target)
        return deleted

    def compact(self) -> int:
        """
        Packs any not-yet-chunked artifacts and garbage-collects the pool.

        Walks every node packing loose artifact files into the shared chunk pool,
        then runs `gc` to delete chunks no node references any more. Its main use
        is migrating a store opened with `chunk=True` whose older nodes were
        written before chunking — those still have whole files on disk.

        Note: reading a packed artifact does not leave a copy in the node
        directory — reassembled bytes go to the disposable read cache (see
        `clear_cache`) — so, unlike earlier versions, you do not need to call
        `compact` to reclaim space after reading. It remains useful for the
        migration case and to trigger a `gc`.

        Returns:
            int: The number of chunks deleted by the subsequent garbage collection.
        """
        for entry in self.root.iterdir():
            if entry.is_dir() and (entry / "meta.json").exists():
                node = self.get_node(entry.name)
                if node:
                    node._pack()
        return self.gc()

    def gc(self) -> int:
        """
        Deletes chunks in the shared pool that no node references any more.

        Scans every node's artifact manifest for the chunks still in use, then
        removes the rest. A store-level lock makes concurrent collections safe,
        and chunks written in the last minute are spared so an in-flight pack in
        another process is never reaped before it records its recipe.

        Returns:
            int: The number of chunks deleted.
        """
        lock = self.root / ".gc.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return 0  # another collection holds the lock; nothing to do
        try:
            live: set[str] = set()
            for entry in self.root.iterdir():
                manifest = entry / ARTIFACT_MANIFEST
                if not manifest.exists():
                    continue
                try:
                    records = json.loads(manifest.read_text())
                except json.JSONDecodeError:
                    continue
                for record in records.values():
                    live.update(record.get("chunks", ()))

            store = ChunkStore(self.root)
            grace = time.time() - 60
            removed = 0
            for digest in list(store.all_digests()):
                if digest not in live and store.mtime(digest) < grace:
                    store.delete(digest)
                    removed += 1
            return removed
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    # ---------------------------------------------------------------------------
    # Visualisation
    # ---------------------------------------------------------------------------

    def generate_web_graph(self) -> Path:
        """
        Creates an interactive web graph of node hierarchies and lineage.

        Renders every node in the store as a self-contained HTML file — all styles and scripts are inlined, so the file can be opened directly in a browser or shared as-is. Nodes are laid out by lineage and coloured by step type; clicking a node reveals its metadata and artifacts.

        The file is written to `<store root>/interactive_pipeline.html`, and the location is printed on completion.
        """
        path = run_web_generator(self)
        return path

    # def host_live_graph(self):
    #     start_ui(self)
