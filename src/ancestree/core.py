# Python packages
from pathlib import Path
import json
import time
import uuid
from typing import List, Dict, Any, Optional, Union, Iterator
import shutil
from contextlib import contextmanager
import warnings

# Internal dependancies
from .database import lineage_database
from .models import Node
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
        rules: Union[Dict, None] = None,
        gen_triggers: Union[List, None] = None,
    ):
        """
        Initialises the LineageStore, ensures its directory exists, and loads or creates the ruleset configuration.

        On creation the LineageStore saves a .lineage_config.json file. On subsequent re-creation, the store reads from this file. There is no need to resupply rules or gen_triggers at any point after initial creation even if the store no longer exists in memory. The rules and gen_triggers cannot be changed after initial creation.

        Args:
            root (Union[Path, str]): Root directory for data pipeline. This is where the nodes sit.
            rules (Dict, optional): A mapping defining the allowed transitions. Defaults to None.
            gen_triggers (List, optional): Step types that mark a new generation. When a node of this type is created, its generation number increments by one relative to its parent. Defaults to None.

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
        self.database = lineage_database(self.root)

    def _do_config(
        self, supplied_rules: Optional[Dict], supplied_triggers: Optional[List]
    ) -> Dict[str, Any]:
        if not self.config_path.exists():
            # Atomic create: a concurrent reader must never see a partially
            # written config, and the temp name must be unique per writer.
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
                stacklevel=2,
            )

    def _persist_if_touched(self, node: "Node", healthy: bool, duration: float) -> bool:
        """
        Persists and indexes the node if the user wrote any artifact or
        metadata, recording whether its code block ran to completion in the
        'healthy' flag, how long the block took in 'duration_s', and the
        total size of its files in 'size_mb'. Returns True if the node was
        persisted.
        """
        has_artifacts = bool(node.artifacts())
        has_user_meta = bool(set(node._metadata) - node._system_keys)
        if not (has_artifacts or has_user_meta):
            return False
        size = sum(f.stat().st_size for f in node.path.rglob("*") if f.is_file())
        node.add_meta(
            "healthy", healthy, data_type="text", group="Structural Properties"
        )
        node.add_meta(
            "duration_s",
            round(duration, 3),
            data_type="text",
            group="Structural Properties",
        )
        node.add_meta(
            "size_mb",
            round(size / 1e6, 6),
            data_type="text",
            group="Structural Properties",
        )
        node._write_meta()
        self.database.add(node.node_id, node.to_db())
        return True

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
        str_id = self.database.get_most_recent(**kwargs)
        return self._node_from_index(str_id) if str_id else None

    def from_parent(self, node: Union[str, "Node"], filename: str) -> List[Path]:
        """
        Shortcut to get specific file(s) from the parent node of the specified node.

        Equivalent to looking up the node's parent and calling `artifacts` on it. The typical use is reading the previous step's output as the current step's input.

        Args:
            node (Union[str, Node]): The Node or node_id whose parent to search.
            filename (str): A glob pattern or substring to match against the parent's files, as in `Node.artifacts`.

        Returns:
            List[Path]: The matching file paths from the parent node, relative to the store root. Empty if the node or its parent cannot be found, the node has no parent, or nothing matches.

        Examples:
            >>> with store.create_node(step_type="model", parent=clean_node) as node:
            ...     [training_data] = store.from_parent(node, "cleaned.csv")
        """
        node = self.get_node(node)
        if node is None or node.parent_id is None:
            return []
        parent_node = self.get_node(node.parent_id)
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
        target = self.get_node(node)
        if not target:
            return []

        if target.path.resolve() == self.root.resolve():
            raise PermissionError("Cannot prune the root lineageStore directory.")

        deleted = []
        for child in self.get_child_nodes(target):
            deleted.extend(self.prune(child, dry_run=dry_run))

        if not dry_run:
            shutil.rmtree(target.path)
            self.database.remove(target.node_id)

        deleted.append(target)
        return deleted

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
        print(f"Graph generated at {path}")
        return path

    # def host_live_graph(self):
    #     start_ui(self)
