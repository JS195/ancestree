# Python packages
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Dict, Union, Optional, Literal
from copy import deepcopy

# Internal dependancies
from .utils import get_provenance, is_pandas

_DataType = Literal["auto", "image", "link", "table", "json", "code", "text"]
_VALID_DATA_TYPES = {"auto", "image", "link", "table", "json", "code", "text"}


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

    def __init__(
        self,
        path: Path,
        node_id: str,
        generation: int,
        parent_id: str,
        step_type: str = None,
    ):
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
    def _from_index(cls, path: Path, flat: dict) -> "Node":
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
        node = cls(
            path,
            flat.get("node_id"),
            flat.get("generation"),
            flat.get("parent_id"),
            step_type=flat.get("step_type"),
        )
        node._metadata = None
        return node

    @classmethod
    def _load(cls, path: Path) -> "Node":
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
            return meta.get(key).get("value")

        node = cls(
            path,
            _value("node_id"),
            _value("generation"),
            _value("parent_id"),
            step_type=_value("step_type"),
        )
        node._metadata = meta
        return node

    @classmethod
    def _create(
        cls,
        path: Path,
        node_id: str,
        generation: int,
        parent_id: str,
        step_type: str = None,
    ) -> "Node":
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
        node.add_meta(
            "node_id", node_id, data_type="text", group="Structural Properties"
        )
        node.add_meta(
            "parent_id", parent_id, data_type="text", group="Structural Properties"
        )
        node.add_meta(
            "generation", generation, data_type="text", group="Structural Properties"
        )
        node.add_meta(
            "step_type", step_type, data_type="text", group="Structural Properties"
        )
        node.add_meta(
            "timestamp",
            datetime.now(timezone.utc).isoformat(),
            data_type="text",
            group="Structural Properties",
        )
        for key, value in get_provenance().items():
            node.add_meta(
                key, value, data_type="text", group="Provenance", searchable=False
            )
        node._system_keys = set(node._metadata)
        return node

    #: Path suffixes rendered inline as images when the data_type is inferred.
    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

    @staticmethod
    def _infer_data_type(value: Any) -> str:
        """
        Maps a value to its natural data_type for 'auto' mode. Only types
        that are unambiguous signals are inferred: DataFrames, dicts/lists,
        Path objects (a Path is a deliberate file reference, never prose),
        and URL strings. Plain strings always stay 'text' — sniffing string
        contents is how auto-detection produces surprises.
        """
        if is_pandas(value):
            return "table"
        if isinstance(value, (dict, list)):
            return "json"
        if isinstance(value, Path):
            return "image" if value.suffix.lower() in Node.IMAGE_SUFFIXES else "link"
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return "link"
        return "text"

    def add_meta(
        self,
        key: str,
        value: Any,
        group: Optional[str] = "General",
        data_type: _DataType = "auto",
        searchable: bool = True,
    ) -> None:
        """
        Attaches a piece of metadata to the node.

        Metadata is what makes a node discoverable: every searchable entry can be matched by the store's search methods (`find_node`, `find_in_lineage`, `get_most_recent_node`) and is displayed in the interactive web graph. Adding a key that already exists overwrites the previous entry.

        Args:
            key (str): The name of the metadata entry.
            value (Any): The value to store. Must be JSON-serialisable.
            group (str, optional): A heading to group related entries under in the web graph display. Defaults to General.
            data_type (str, optional): How the value is rendered in the web graph. The default 'auto' infers it from the value: pandas DataFrames render as tables, dicts and lists as formatted JSON, Path objects as inline images (image suffixes) or file links, http(s) strings as links, and everything else as plain text. Pass a type explicitly to override the inference: 'image' for a path to an image file inside the node (the value is rewritten relative to the store root), 'link' for a clickable link, 'table' for a pandas DataFrame (stored as columns/rows), 'json' for a dict or list, 'code' for a monospaced snippet (e.g. the SQL or shell command that produced the node), or 'text' for plain text.
            searchable (bool, optional): If True the entry is indexed and can be matched by the store's search methods. Set to False for display-only metadata. Defaults to True.

        Examples:
            >>> with store.create_node(step_type="model", parent=clean_node) as node:
            ...     # Plain values — searchable by default
            ...     node.add_meta("accuracy", 0.94, group="Metrics")
            ...     node.add_meta("epochs", 42, group="Metrics")
            ...     node.add_meta("learning_rate", 1e-3, group="Config")
            ...
            ...     # Dict/list — rendered as formatted JSON in the web graph
            ...     node.add_meta("params", {"optimizer": "adam", "dropout": 0.3}, group="Config")
            ...
            ...     # Table — pandas DataFrame rendered as a sortable table; not searchable
            ...     node.add_meta("results", df, data_type="table", group="Outputs")
            ...
            ...     # Image — path rewritten relative to store root, rendered inline
            ...     fig.savefig(node / "confusion.png")
            ...     node.add_meta("confusion_matrix", node / "confusion.png", group="Figures")
            ...
            ...     # Code — rendered in a monospaced block
            ...     node.add_meta("query", "SELECT * FROM runs WHERE status = 'ok'", data_type="code")
            ...
            ...     # Display-only — visible in the web graph but excluded from find_node
            ...     node.add_meta("notes", "rerun after fixing label encoding", searchable=False)
            ...
            ...     # External link — rendered as a clickable URL
            ...     node.add_meta("wandb_run", "https://wandb.ai/my-org/run/abc123", group="Links")
        """
        if data_type not in _VALID_DATA_TYPES:
            raise ValueError(
                f"Invalid data_type {data_type!r}. "
                f"Must be one of: {', '.join(sorted(_VALID_DATA_TYPES))}"
            )

        if data_type == "auto":
            data_type = self._infer_data_type(value)

        if data_type in ("image", "link") and not str(value).startswith(
            ("http://", "https://")
        ):
            # Store file references relative to the store root so they
            # resolve from the generated HTML. URLs pass through untouched —
            # Path() would collapse their double slash.
            value = str(
                Path(
                    str(value)
                    .removeprefix(str(self.path.parent) + "/")
                    .removeprefix(str(self.path.parent))
                )
            )
            searchable = False

        if data_type == "table":
            if not is_pandas(value):
                raise TypeError(
                    f"Expected a pandas DataFrame for 'table', got {type(value).__name__}"
                )
            split = value.to_dict(orient="split")
            value = {"columns": split["columns"], "rows": split["data"]}
            searchable = False

        if data_type == "json":
            if not isinstance(value, (dict, list)):
                raise TypeError(
                    f"Expected a dict or list for 'json', got {type(value).__name__}"
                )
            try:
                json.dumps(value)
                searchable = False
            except (TypeError, ValueError) as e:
                # Fail here, at the call site, rather than corrupting the
                # node's whole meta.json write later.
                raise TypeError(
                    f"Value for 'json' is not JSON-serialisable: {e}"
                ) from None

        entry = {
            f"{key}": {
                "value": value,
                "data_type": data_type,
                "group": group,
                "searchable": searchable,
            }
        }

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
            if properties.get("searchable", True):
                entries[key] = properties.get("value")

        return entries

    def artifacts(self, contains: str = "*") -> List[Path]:
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
                if (
                    f.match(search_pattern)
                    or f.name.lower().find(contains.lower()) != -1
                ):
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
        target_path = self.path / relative_loc
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
