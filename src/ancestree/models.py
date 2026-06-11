import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Dict
from .utils import get_provenance
from copy import deepcopy
from typing import Union

class Node:
    """
    Represents a single step in the pipeline: a directory on disk holding the step's artifacts and a meta.json describing it.

    Nodes are not constructed directly. They are created by `LineageStore.create_node` and returned by the store's search and lineage methods (`find_node`, `get_lineage`, `get_child_nodes`, ...). Interact with a node to read and attach metadata, locate its artifacts, and build paths inside its directory with the `/` operator.

    Attributes:
        path (Path): The node's directory on disk.
        node_id (str): The unique 8-character identifier of the node.
        generation (int): The generation number of the node in the pipeline.
        parent_id (str): The node_id of the parent node, or None for a root node.
        step_type (str): The type of pipeline step this node represents.
    """
    def __init__(self, path: Path, node_id: str, generation: int, parent_id: str, step_type:str=None):
        """
        Initialises a node instance. Performs no I/O: use Node._load() to read
        an existing node from disk or Node._create() to initialise a new one.

        Args:
            path (Path): The filesystem path where the node is stored.
            node_id (str): A unique 8-character alphanumeric identifier.
            generation (int): The generation number of the node in the pipeline.
            parent_id (str): The unique 8-character identifier of node the current node descends from.
        """
        self.path = path
        self.node_id = node_id
        self.generation = generation
        self.parent_id = parent_id
        self.step_type = step_type
        self._metadata = {}
        self._system_keys = set[Any]()

    @property
    def metadata(self) -> Dict[str, Dict[str, Any]]:
        """
        The node's full metadata as a dictionary.

        Each key maps to an entry of the form `{'value': ..., 'type': ..., 'group': ..., 'searchable': ...}`. This includes both the structural metadata written by the store (node_id, parent_id, generation, step_type, timestamp, provenance) and anything added with `add_meta`.

        Returns a deep copy: mutating the result does not change the node. Use `add_meta` to modify metadata.

        Returns:
            Dict[str, Dict]: A mapping of metadata key to its entry dictionary.

        Examples:
            >>> node.metadata["accuracy"]["value"]
            0.92
        """
        return deepcopy(self._hydrate())

    def _hydrate(self):
        # Index-backed nodes (_from_index) defer reading meta.json until the
        # full metadata is actually needed.
        if self._metadata is None:
            self._metadata = json.loads((self.path / "meta.json").read_text())
        return self._metadata

    @classmethod
    def _from_index(cls, path: Path, flat: dict) -> 'Node':
        """
        Builds a node from the database's flattened index entry without
        touching disk. The full metadata is hydrated lazily from meta.json
        on first access.

        Args:
            path (Path): The directory of the node.
            flat (dict): The node's flat index entry (key -> value).

        Returns:
            Node: The index-backed node.
        """
        node = cls(path, flat.get('node_id'), flat.get('generation'),
                   flat.get('parent_id'), step_type=flat.get('step_type'))
        node._metadata = None
        return node

    @classmethod
    def _load(cls, path: Path) -> 'Node':
        """
        Loads an existing node from its meta.json, which is the single source
        of truth for the structural attributes.

        Args:
            path (Path): The directory of the node to load.

        Returns:
            Node: The loaded node.
        """
        meta = json.loads((path / "meta.json").read_text())
        def _value(key):
            return meta.get(key).get('value')
        node = cls(path, _value('node_id'), _value('generation'),
                   _value('parent_id'), step_type=_value('step_type'))
        node._metadata = meta
        return node

    @classmethod
    def _create(cls, path: Path, node_id: str, generation: int, parent_id: str, step_type:str=None) -> 'Node':
        """
        Initialises a brand new node with its structural and provenance
        metadata. The initial keys are recorded in _system_keys so the store
        can tell system-written metadata apart from user additions.

        Args:
            path (Path): The filesystem path where the node is stored.
            node_id (str): A unique 8-character alphanumeric identifier.
            generation (int): The generation number of the node in the pipeline.
            parent_id (str): The unique 8-character identifier of node the current node descends from.

        Returns:
            Node: The new node. Nothing is written to disk until _write_meta().
        """
        node = cls(path, node_id, generation, parent_id, step_type=step_type)
        node.add_meta('node_id', node_id, type='text', group='Structural Properties')
        node.add_meta('parent_id', parent_id, type='text', group='Structural Properties')
        node.add_meta('generation', generation, type='text', group='Structural Properties')
        node.add_meta('step_type', step_type, type='text', group='Structural Properties')
        node.add_meta('timestamp', datetime.now(timezone.utc).isoformat(), type='text', group='Structural Properties')
        for key, value in get_provenance().items():
            node.add_meta(key, value, type='text', group='Provenance', searchable=False)
        node._system_keys = set(node._metadata)
        return node


    def add_meta(self, key, value, type='text', group=None, searchable=True):
        """
        Attaches a piece of metadata to the node.

        Metadata is what makes a node discoverable: every searchable entry can be matched by the store's search methods (`find_node`, `find_in_lineage`, `get_most_recent_node`) and is displayed in the interactive web graph. Adding a key that already exists overwrites the previous entry.

        Args:
            key (str): The name of the metadata entry.
            value (Any): The value to store. Must be JSON-serialisable.
            type (str, optional): How the value is rendered in the web graph. Use 'image' for a path to an image file inside the node (the value is rewritten relative to the store root and displayed inline) or 'link' for a clickable file link. Any other value, e.g. the default 'text', renders as plain text. Defaults to 'text'.
            group (str, optional): A heading to group related entries under in the web graph display. Defaults to None.
            searchable (bool, optional): If True the entry is indexed and can be matched by the store's search methods. Set to False for display-only metadata. Defaults to True.

        Examples:
            >>> with store.create_node(step_type="model") as node:
            ...     node.add_meta("accuracy", 0.92, group="Metrics")
            ...     node.add_meta("loss_curve", node / "loss.png", type="image", group="Metrics")
        """
        if type == 'image':
            value = str(Path(str(value).removeprefix(str(self.path.parent) + "/").removeprefix(str(self.path.parent))))
        entry = {f'{key}': {
            'value': value,
            'type': type,
            'group': group,
            'searchable': searchable
        }}
        self._hydrate().update(entry)

    def _write_meta(self):
        """
        Internal helper for creating and writing metadata atomically to prevent corruption during crashes.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        # Atomic write
        try:
            temp_file = self.path / "meta.json.tmp"
            temp_file.write_text(json.dumps(self.metadata, indent=2))
            temp_file.replace(self.path / "meta.json")
        finally:
            if temp_file.exists():
                temp_file.unlink()

    def to_db(self):
        # This is a flat key value dict for easy searching and indexing
        entries = {}
        for key, properties in self._hydrate().items():
            if properties.get('searchable', True):
                entries[key] = properties.get('value')

        return entries


    def artifacts(self, contains:str = "*") -> List[Path]:
        """
        Searches this node's directory returning all files excluding internal metadata.
        Recursively finds all artifacts regardless of storage depth.

        Args:
            contains (str, optional): A glob pattern to filter discovered files. A plain substring also works: it is matched anywhere in the filename, case-insensitively. Defaults to "*" (all files).

        Returns:
            List[Path]: The matching file paths, relative to the store root.

        Examples:
            >>> node.artifacts("*.csv")
            [PosixPath('abc12345/sample.csv')]
            >>> node.artifacts("sample")
            [PosixPath('abc12345/sample.csv')]
        """
        artifacts = []

        search_pattern = contains
        if "*" not in contains and "?" not in contains:
            search_pattern = f"*{contains}*"
        
        for f in self.path.rglob("*"):
            if f.is_file() and f.name != "meta.json":
                if f.match(search_pattern) or f.name.lower().find(contains.lower()) != -1:
                    artifacts.append(f.relative_to(self.path.parent))
        return artifacts
            
    def __truediv__(self, relative_loc: Union[Path, str]) -> Path:
        """
        Allows the use of the '/' operator to create paths inside the node's directory, mirroring pathlib.

        This is the idiomatic way to choose where to write an artifact. Any intermediate directories in the path are created automatically, so the returned path is always ready to write to.

        Args:
            relative_loc (Union[Path, str]): The string or Path object to append to the node's base path.

        Returns:
            Path: The resolved destination inside the node's directory.

        Examples:
            >>> with store.create_node(step_type="clean") as node:
            ...     df.to_csv(node / "results/cleaned.csv")
        """
        target_path = self.path/relative_loc
        target_path.parent.mkdir(parents=True, exist_ok=True)
        return target_path

    def __repr__(self):
        """
        Returns a developer friendly string representation of the node.

        Examples:
            >>> node = store.get_node("abc12345")
            >>> print(node)
            'Node = abc12345, path = abc12345, generation = 0'
        """
        return f"Node = {self.node_id}, path = {self.path.name}, generation = {self.generation}"