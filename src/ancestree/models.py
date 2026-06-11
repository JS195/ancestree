import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from .utils import get_provenance
from copy import deepcopy
from typing import Union

class Node:
    def __init__(self, path: Path, node_id: str, generation: int, parent_id: str, step_type:str=None):
        """
        Initialises a node instance and ensures its directory exists.

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

        self._spin_up_node()

    @property
    def metadata(self):
        return deepcopy(self._metadata)

    def _spin_up_node(self):
        meta_path = self.path / "meta.json"
        if meta_path.exists():
            # If the node exists we load the metadata
            self._metadata = json.loads((meta_path).read_text())
        else:
            # If the node does not yet exist we must write its properties to the metadata
            self.add_meta('node_id', self.node_id, type='text', group='Structural Properties')
            self.add_meta('parent_id', self.parent_id, type='text', group='Structural Properties')
            self.add_meta('generation', self.generation, type='text', group='Structural Properties')
            self.add_meta('step_type', self.step_type, type='text', group='Structural Properties')
            self.add_meta('timestamp', datetime.now(timezone.utc).isoformat(), type='text', group='Structural Properties')
            for key, value in get_provenance().items():
                self.add_meta(key, value, type='text', group='Provenance', searchable=False)


    def add_meta(self, key, value, type='text', group=None, searchable=True):
        if type == 'image':
            value = str(Path(str(value).removeprefix(str(self.path.parent) + "/").removeprefix(str(self.path.parent))))
        entry = {f'{key}': {
            'value': value,
            'type': type,
            'group': group,
            'searchable': searchable
        }}
        self._metadata.update(entry)

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
        for m in self.metadata.keys():
            nested_properties = self.metadata.get(m)
            if nested_properties.get('searchable', True):
                entries[m] = nested_properties.get('value')
        
        return entries


    def artifacts(self, contains:str = "*") -> List[Path]:
        """
        Searches this node's directory returning all files excluding internal metadata.
        Recursively finds all artifacts regardless of storage depth.

        Args:
            contains (str, optional): A glob pattern to filter discovered files. Defaults to "*".

        Returns:
            List[Path]: A list of dictionaries containing file metadata including name, absolute path, and extension (file type).
        
        Examples:
            >>> node.artifacts("*.csv")
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
            
    def __truediv__(self, relative_loc: Union[Path, str]):
        """
        Allows the use of the '/' operator to create paths relative to the node.

        Args:
            relative_loc (Union[Path, str]): The string or Path object to append to the node's base path.

        Returns (Path):
            A path object representing the desired destination.

        Examples:
            >>> node = store.get_node("abc12345")
            >>> data_path = node / "results/some_data.csv"
            Path('store/abc12345/results/some_data.csv')
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