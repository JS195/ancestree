# Internal packages
from pathlib import Path
import json
import uuid
from typing import List, Dict, Any, Optional, Union
import shutil
from contextlib import contextmanager

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
    def __init__(self, root: Union[Path, str], rules: Union[Dict, None] = None, gen_triggers: Union[List, None] = None):
        """
        Initialises the LineageStore, ensures its directory exists, and loads or creates the ruleset configuration.

        On creation the LineageStore saves a .lineage_config.json file. On subsequent re-creation, the store reads from this file. There is no need to resupply rules or gen_triggers at any point after initial creation even if the store no longer exists in memory. The rules and gen_triggers cannot be changed after initial creation. 

        Args:
            root (Union[Path, str]): Root directory for data pipeline. This is where the nodes sit.
            rules (Dict, optional): A mapping defining the allowed transitions. Defaults to None.
            gen_triggers (List, optional): List of step types that when reached increment the node's generation. Defaults to None.
        
        Examples:
            >>> rules = {"clean": ["ingest"], "model":["clean"]}
            >>> store = LineageStore("my_project", rules=rules, triggers=["ingest"])
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        self.config_path = self.root/".lineage_config.json"
        config = self._do_config(rules, gen_triggers)
        self.rules = config["rules"]
        self.triggers = config["triggers"]
        self.database = lineage_database(self.root)

    def _do_config(self, supplied_rules: Optional[Dict], supplied_triggers: Optional[List]) -> Dict[str, Any]:
        if not self.config_path.exists():
            self.config_path.write_text(json.dumps({
                "rules": supplied_rules,
                "triggers": supplied_triggers
            }, indent=2))

        config = json.loads(self.config_path.read_text())
        return {
            "rules": config.get("rules") or {},
            "triggers": config.get("triggers") or []
        }

    def find_node(self, **kwargs: Any) -> List['Node']:
        """
        Search for nodes based on metadata key values. To simply find a key regardless of the value it holds the keyword can be specified with an ellipsis value.

        Args:
            **kwargs (Any): Key-value pairs to match against node metadata (searches top level and nested dict entries).

        Returns:
            List['Node']: A list of node objects that match all provided criteria.
        
        Examples:
            >>> store.find_node(step_type="ingest")
            >>> store.find_node(accuracy>0.8, generation<3)
        """
        node_objs = []
        nodes = self.database.find_matches(**kwargs)
        for node in nodes:
            node_objs.append(self.get_node(node))
        return node_objs

    def get_node(self, node: Union[str, 'Node', None] = None) -> Optional['Node']:
        """
        Method to check if argument supplied is a Node. If not it resolves str into a Node object.

        Args:
            node: Can be a Node instance, str node_id, or None.

        Returns:
            'Node': The resolved Node object or None if the input is invalid or not found.
        
        Examples:
            >>> store.get_node("abc12345")
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

    @contextmanager
    def create_node(self, step_type:str, parent: Union['Node', str, None] = None):
        """Creates a new node while enforcing lineage rules.

        The node only materialises on disk once the user writes an artifact or
        adds metadata; an untouched node is discarded with a warning. If the
        user's code raises after writing, the partial work is persisted and the
        node's 'healthy' metadata flag is set to False (True on clean completion).

        If parent is not supplied, the store will search the most recently created node to serve as a parent subject to the lineage rules.

        Args:
            step_type (str): The type of pipeline step being performed. 
            parent ('Node' | str, optional): The parent Node object or node_id. Defaults to None.
            extra_metadata (Dict, optional): Other arbitrary extra_metadata to store in the node's 'data' field. Defaults to None.

        Raises:
            ValueError: If the step type transition is not permitted according to the store rules.

        Yields:
            'Node': A new node instance.
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
            current_gen = parent_gen+1
        else:
            current_gen=parent_gen

        node_id = uuid.uuid4().hex[:8]
        while node_id in self.database.cache or (self.root / node_id).exists():
            node_id = uuid.uuid4().hex[:8]
        node_path = self.root / node_id

        parent_id = parent_node.node_id if parent_node else None
        new_node = Node._create(node_path, node_id, current_gen, parent_id, step_type=step_type)

        try:
            yield new_node
        except BaseException:
            # Keep partial work: anything written before the failure persists,
            # flagged as unhealthy. An untouched node leaves no trace.
            if not self._persist_if_touched(new_node, healthy=False):
                shutil.rmtree(new_node.path, ignore_errors=True)
            raise

        if not self._persist_if_touched(new_node, healthy=True):
            shutil.rmtree(new_node.path, ignore_errors=True)
            import warnings
            warnings.warn(
                f"Node '{new_node.node_id}' (step_type='{step_type}') was discarded: "
                "no artifacts were written and no metadata was added. "
                "Write at least one file or call node.add_meta() to persist the node.",
                UserWarning,
                stacklevel=2
            )

    def _persist_if_touched(self, node: 'Node', healthy: bool) -> bool:
        """
        Persists and indexes the node if the user wrote any artifact or
        metadata, recording whether its code block ran to completion in the
        'healthy' flag. Returns True if the node was persisted.
        """
        has_artifacts = bool(node.artifacts())
        has_user_meta = bool(set(node._metadata) - node._system_keys)
        if not (has_artifacts or has_user_meta):
            return False
        node.add_meta('healthy', healthy, type='text', group='Structural Properties')
        node._write_meta()
        self.database.add(node.node_id, node.to_db())
        return True

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

    def get_lineage(self, node: Union[str, 'Node']) -> List['Node']:
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
        str_ids = self.database.get_lineage(node)
        node_objs = []
        for node in str_ids:
            node_objs.append(self.get_node(node))
        return node_objs

    def find_in_lineage(self, node: Union[str, 'Node'], **kwargs: Any)-> List['Node']:
        """
        Searches a node's ancestry for nodes matching specified search parameters.

        Args:
            node (Union[str, 'Node', None], optional): The Node or node_id whose history to search. If None, the store will use the most recent node. Defaults to None.
            **kwargs (Any): Key-value pairs to match against node metadata (searches top level and nested dict entries).

        Returns:
            List[Path]: A list of matching Node objects.
        """
        if isinstance(node, Node):
            node = node.node_id
        matching_ids = self.database.find_in_lineage(node, **kwargs)
        return [self.get_node(node_id) for node_id in matching_ids]

    def get_most_recent_node(self, **kwargs: Any) -> Optional['Node']:
        """
        Finds and returns the most recent node subject to some keyword arguments. These arguments are used to search the metadata for matching parameters.
        
        Args:
            **kwargs (Any): Key-value pairs to match against node metadata (searches top level and nested dict entries).
        
        Returns:
            List['Node']: A list of all matching nodes. 
        """
        str_id = self.database.get_most_recent(**kwargs)
        return self.get_node(str_id)
    
    def from_parent(self, node: Union[str, 'Node'], filename: str) -> List[Path]:
        """
        Shortcut to get a specific file(s) from the parent node of the specified node.

        Args:
            node (Union[str, 'Node']): The specified node
            filename (str): The file string to match to get from the parent

        Returns:
            List[Path]: A list of file paths from the parent node
        """
        node = self.get_node(node)
        if node.parent_id is None:
            return []
        parent_node = self.get_node(node.parent_id)
        return parent_node.artifacts(filename)
    
    def get_child_nodes(self, node: Union[str, 'Node']) -> List['Node']:
        """
        Returns the offspring nodes of the specified node.

        Args:
            node (Union[str, 'Node']): A 'Node' object or node id string.

        Returns:
            List['Node']: A list of all child nodes.
        """
        target = self.get_node(node)
        return self.find_node(parent_id=target.node_id) if target else []

    def prune(self, node: Union[str, 'Node'], recursive: bool = True, dry_run: bool = True) -> None:
        """
        Delete a node and optionally all children. Searches recursively and purges the entire branch.

        Args:
            node (Union[str, 'Node']): Either a node ID sting or a node object.
            recursive (bool): Whether to prune everything downstream as well. This will delete all nodes that can trace their lineage back to the selected node. Defaults to True.
            dry_run (bool): Print a list of what will be deleted without deleting anything. Must be manually set to False to delete nodes.
            
        Returns:
            None
        """
        target = self.get_node(node)
        if not target:
            return None
        
        if target.path.resolve() == self.root.resolve():
            raise PermissionError("Cannot prune the root lineageStore directory.")

        if recursive:
            for child in self.get_child_nodes(target):
                self.prune(child, recursive=True, dry_run=dry_run)
        
        if not dry_run:
            shutil.rmtree(target.path)
            self.database.remove(target.node_id)
        else:
            print(f"Would delete: {target}")
        return None

    def generate_web_graph(self):
        """
        Create interactive web graph of node hierarchies and lineage.
        """
        path = run_web_generator(self)
        print(f"Graph generated at {path}")
        
    # def host_live_graph(self):
    #     start_ui(self)