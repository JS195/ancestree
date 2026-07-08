"""MetadataStore — node/edge/metadata rows and every metadata query.

The persistence-layer view of a node: ``NodeRecord`` (the node table's
columns plus ordered parent ids) and ``MetadataRow`` (one user-metadata
envelope entry) are frozen value objects; ``MetadataStore`` writes them in
single transactions and answers every query the store exposes.

Query semantics carried over from 0.1.x:

* ``find`` matches every filter (AND). Non-callable values compare by
  equality — numerically via the indexed ``num_value`` column for numbers,
  by canonical JSON text otherwise. Callable values run as predicates in
  Python over the SQL-narrowed candidates; a missing key hands the
  predicate None, and a raising predicate warns and treats the node as
  non-matching.
* multi-key metadata filters intersect per-key subqueries — the trickiest
  SQL in the build, deliberately confined to this one module.
* ``lineage`` returns ancestors + self, oldest first, each node once and
  only after all of its parents (a recursive CTE gathers the closure; the
  post-order walk and cycle guard run in Python).

See REBUILD_BLUEPRINT.md section 5.3 (Phase 2, issue #13).
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..errors import IntegrityError, NodeNotFound
from .connection import ConnectionManager

#: node-table columns that ``find`` filters in SQL. Provenance columns are
#: deliberately absent — provenance was not searchable in 0.1.x either.
_NODE_COLUMNS = frozenset(
    {
        "node_id",
        "step_type",
        "generation",
        "healthy",
        "duration_s",
        "size_bytes",
        "content_hash",
        "created_utc",
        "created_epoch",
    }
)


def encode_value(value: Any) -> str:
    """The canonical JSON encoding for a metadata value. One encoder for
    writes and equality queries, so text comparison is exact."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _numeric(value: Any) -> Optional[float]:
    """The numeric index value for a metadata value (bool is not a
    number here, despite being an int subclass)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class NodeRecord:
    """One row of the node table, plus the node's ordered parent ids."""

    node_id: str
    step_type: str
    generation: int
    created_utc: str
    created_epoch: float
    healthy: bool
    duration_s: Optional[float] = None
    size_bytes: int = 0
    content_hash: Optional[str] = None
    prov_user: Optional[str] = None
    prov_python: Optional[str] = None
    prov_platform: Optional[str] = None
    prov_git_commit: Optional[str] = None
    prov_git_branch: Optional[str] = None
    prov_git_dirty: Optional[bool] = None
    parent_ids: Tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class MetadataRow:
    """One user-metadata entry as persisted: the envelope fields with the
    value in canonical JSON. Build via :func:`metadata_row`."""

    key: str
    value_json: str
    data_type: str
    group: Optional[str]
    searchable: bool
    num_value: Optional[float]

    @property
    def value(self) -> Any:
        """The decoded Python value."""
        return json.loads(self.value_json)


def metadata_row(
    key: str,
    value: Any,
    data_type: str = "text",
    group: Optional[str] = "General",
    searchable: bool = True,
) -> MetadataRow:
    """Builds a MetadataRow from a plain Python value: canonical JSON for
    the text column, plus the extracted numeric for the range index. The
    envelope-level validation and type inference live in domain/metadata.py
    (Phase 4); this is purely the persistence encoding."""
    return MetadataRow(
        key=key,
        value_json=encode_value(value),
        data_type=data_type,
        group=group,
        searchable=searchable,
        num_value=_numeric(value),
    )


class MetadataStore:
    """Reads and writes nodes, edges and metadata in one SQLite store."""

    def __init__(self, manager: ConnectionManager) -> None:
        self._manager = manager

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_node(
        self, record: NodeRecord, metadata: Sequence[MetadataRow] = ()
    ) -> None:
        """Persists a node — its row, its parent edges (order preserved via
        ordinal) and its metadata rows — in one transaction: the node
        appears to every reader wholly or not at all.

        Raises:
            sqlite3.IntegrityError: If the node_id already exists or a
                parent id does not (foreign keys are enforced).
        """
        with self._manager.write() as conn:
            conn.execute(
                "INSERT INTO node (node_id, step_type, generation, "
                "created_utc, created_epoch, healthy, duration_s, "
                "size_bytes, content_hash, prov_user, prov_python, "
                "prov_platform, prov_git_commit, prov_git_branch, "
                "prov_git_dirty) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (
                    record.node_id,
                    record.step_type,
                    record.generation,
                    record.created_utc,
                    record.created_epoch,
                    int(record.healthy),
                    record.duration_s,
                    record.size_bytes,
                    record.content_hash,
                    record.prov_user,
                    record.prov_python,
                    record.prov_platform,
                    record.prov_git_commit,
                    record.prov_git_branch,
                    None
                    if record.prov_git_dirty is None
                    else int(record.prov_git_dirty),
                ),
            )
            conn.executemany(
                "INSERT INTO edge (child_id, parent_id, ordinal) "
                "VALUES (?, ?, ?)",
                [
                    (record.node_id, parent_id, ordinal)
                    for ordinal, parent_id in enumerate(record.parent_ids)
                ],
            )
            conn.executemany(
                "INSERT INTO metadata (node_id, key, value, data_type, grp, "
                "searchable, num_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record.node_id,
                        row.key,
                        row.value_json,
                        row.data_type,
                        row.group,
                        int(row.searchable),
                        row.num_value,
                    )
                    for row in metadata
                ],
            )

    def remove(self, node_id: str) -> None:
        """Deletes a node row; edges and metadata cascade. A missing id is
        a no-op (matching 0.1.x). DAG-aware pruning of descendants is the
        Pruner's job (Phase 5), not this method's."""
        with self._manager.write() as conn:
            conn.execute("DELETE FROM node WHERE node_id = ?", (node_id,))

    # ------------------------------------------------------------------
    # Point reads
    # ------------------------------------------------------------------

    def get(self, node_id: str) -> Optional[NodeRecord]:
        """The node's record, or None if the id is unknown."""
        conn = self._manager.read()
        row = conn.execute(
            "SELECT * FROM node WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        parents = tuple(
            r["parent_id"]
            for r in conn.execute(
                "SELECT parent_id FROM edge WHERE child_id = ? "
                "ORDER BY ordinal",
                (node_id,),
            )
        )
        return NodeRecord(
            node_id=row["node_id"],
            step_type=row["step_type"],
            generation=row["generation"],
            created_utc=row["created_utc"],
            created_epoch=row["created_epoch"],
            healthy=bool(row["healthy"]),
            duration_s=row["duration_s"],
            size_bytes=row["size_bytes"],
            content_hash=row["content_hash"],
            prov_user=row["prov_user"],
            prov_python=row["prov_python"],
            prov_platform=row["prov_platform"],
            prov_git_commit=row["prov_git_commit"],
            prov_git_branch=row["prov_git_branch"],
            prov_git_dirty=None
            if row["prov_git_dirty"] is None
            else bool(row["prov_git_dirty"]),
            parent_ids=parents,
        )

    def exists(self, node_id: str) -> bool:
        row = self._manager.read().execute(
            "SELECT 1 FROM node WHERE node_id = ? LIMIT 1", (node_id,)
        ).fetchone()
        return row is not None

    def metadata_for(self, node_id: str) -> Dict[str, MetadataRow]:
        """Every metadata entry of a node, searchable or not."""
        rows = self._manager.read().execute(
            "SELECT key, value, data_type, grp, searchable, num_value "
            "FROM metadata WHERE node_id = ?",
            (node_id,),
        ).fetchall()
        return {
            row["key"]: MetadataRow(
                key=row["key"],
                value_json=row["value"],
                data_type=row["data_type"],
                group=row["grp"],
                searchable=bool(row["searchable"]),
                num_value=row["num_value"],
            )
            for row in rows
        }

    def all_node_ids(self) -> List[str]:
        """Every node id, oldest first."""
        rows = self._manager.read().execute(
            "SELECT node_id FROM node ORDER BY created_epoch, node_id"
        ).fetchall()
        return [row["node_id"] for row in rows]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find(self, **filters: Any) -> List[str]:
        """The ids of nodes matching every filter, oldest first.

        A filter key is a node column (step_type, generation, healthy,
        ...), the special key ``parent_id`` (matched against the node's
        ordered parent-id list, exactly as 0.1.x's flat index did — an
        empty list matches roots), or a user-metadata key. Non-callable
        values match by equality in SQL; callable values run as Python
        predicates over the SQL-narrowed candidates, receiving the stored
        value or None when the key is absent (or not searchable; a
        ``parent_id`` predicate receives the list, empty for roots). A
        raising predicate warns and treats that node as non-matching. No
        filters returns every node.
        """
        column_eq: List[Tuple[str, Any]] = []
        metadata_eq: List[Tuple[str, Any]] = []
        predicates: List[Tuple[str, Callable[[Any], Any]]] = []
        parent_eq: Optional[List[str]] = None
        parent_filtering = False
        for key, value in filters.items():
            if key == "parent_id":
                # Parents live in the edge table, not a column: matched in
                # Python over the SQL-narrowed candidates.
                parent_filtering = True
                if callable(value):
                    predicates.append((key, value))
                else:
                    parent_eq = [str(item) for item in value]
            elif callable(value):
                predicates.append((key, value))
            elif key in _NODE_COLUMNS:
                column_eq.append((key, value))
            else:
                metadata_eq.append((key, value))

        # One statement: column equality as WHERE terms, each metadata
        # filter as an intersecting per-key subquery on the indexed
        # metadata table.
        conditions: List[str] = []
        params: List[Any] = []
        for column, value in column_eq:
            if value is None:
                conditions.append(f"{column} IS NULL")
            else:
                conditions.append(f"{column} = ?")
                params.append(value)
        for key, value in metadata_eq:
            numeric = _numeric(value)
            if numeric is not None:
                conditions.append(
                    "node_id IN (SELECT node_id FROM metadata WHERE key = ? "
                    "AND searchable = 1 AND num_value = ?)"
                )
                params.extend([key, numeric])
            else:
                conditions.append(
                    "node_id IN (SELECT node_id FROM metadata WHERE key = ? "
                    "AND searchable = 1 AND value = ?)"
                )
                params.extend([key, encode_value(value)])

        sql = "SELECT * FROM node"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_epoch, node_id"
        candidates = self._manager.read().execute(sql, params).fetchall()

        parent_lists: Dict[str, List[str]] = (
            self._parent_lists() if parent_filtering else {}
        )
        if parent_eq is not None:
            candidates = [
                row
                for row in candidates
                if parent_lists.get(row["node_id"], []) == parent_eq
            ]

        if not predicates:
            return [row["node_id"] for row in candidates]

        # Predicate stage: fetch each predicate key's searchable values in
        # one query, then evaluate in Python.
        meta_values: Dict[str, Dict[str, Any]] = {}
        for key, _ in predicates:
            if key in _NODE_COLUMNS or key == "parent_id":
                continue
            rows = self._manager.read().execute(
                "SELECT node_id, value FROM metadata "
                "WHERE key = ? AND searchable = 1",
                (key,),
            ).fetchall()
            meta_values[key] = {
                row["node_id"]: json.loads(row["value"]) for row in rows
            }

        matched: List[str] = []
        for row in candidates:
            node_id = row["node_id"]
            ok = True
            for key, predicate in predicates:
                stored: Any
                if key == "parent_id":
                    stored = parent_lists.get(node_id, [])
                elif key in _NODE_COLUMNS:
                    stored = row[key]
                    if key == "healthy":
                        stored = bool(stored)
                else:
                    stored = meta_values[key].get(node_id)
                try:
                    if not predicate(stored):
                        ok = False
                        break
                except Exception as exc:
                    warnings.warn(
                        f"Predicate for {key!r} raised "
                        f"{type(exc).__name__}: {exc}. Node treated as "
                        "non-matching.",
                        UserWarning,
                        stacklevel=2,
                    )
                    ok = False
                    break
            if ok:
                matched.append(node_id)
        return matched

    def _parent_lists(self) -> Dict[str, List[str]]:
        """Every node's ordered parent ids in one query — backs the
        ``parent_id`` filter, which cannot be a plain column because
        parents live in the edge table."""
        rows = self._manager.read().execute(
            "SELECT child_id, parent_id FROM edge ORDER BY child_id, ordinal"
        ).fetchall()
        parents: Dict[str, List[str]] = {}
        for row in rows:
            parents.setdefault(row["child_id"], []).append(row["parent_id"])
        return parents

    def most_recent(self, node_ids: Sequence[str]) -> Optional[str]:
        """The most recently created of the given ids, or None if empty."""
        if not node_ids:
            return None
        placeholders = ",".join("?" for _ in node_ids)
        row = self._manager.read().execute(
            f"SELECT node_id FROM node WHERE node_id IN ({placeholders}) "
            "ORDER BY created_epoch DESC, node_id DESC LIMIT 1",
            list(node_ids),
        ).fetchone()
        return None if row is None else str(row["node_id"])

    def find_by_hash(self, content_hash: str) -> Optional[str]:
        """A node whose content_hash matches, or None. Backs node-level
        dedup: the caller treats the result as a candidate and
        byte-verifies before reuse."""
        row = self._manager.read().execute(
            "SELECT node_id FROM node WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone()
        return None if row is None else str(row["node_id"])

    # ------------------------------------------------------------------
    # Graph walks
    # ------------------------------------------------------------------

    def children(self, node_id: str) -> List[str]:
        """Direct children (nodes listing `node_id` among their parents),
        in creation order."""
        rows = self._manager.read().execute(
            "SELECT e.child_id FROM edge e JOIN node n ON n.node_id = "
            "e.child_id WHERE e.parent_id = ? "
            "ORDER BY n.created_epoch, n.node_id",
            (node_id,),
        ).fetchall()
        return [row["child_id"] for row in rows]

    def lineage(self, node_id: str) -> List[str]:
        """Every ancestor of `node_id` plus itself, in topological order
        (oldest first): each node appears once, after all of its parents.
        For a linear chain this is the chain; for a DAG join it is the
        union of the inputs' histories.

        Raises:
            NodeNotFound: If `node_id` is not in the store.
            IntegrityError: If the ancestry contains a cycle.
        """
        if not self.exists(node_id):
            raise NodeNotFound(
                f"Node {node_id!r} not found in this store."
            )
        # The recursive CTE gathers the ancestor closure (UNION dedups, so
        # it terminates even on a cyclic graph); the ordering walk and the
        # cycle guard run in Python over that small subgraph.
        rows = self._manager.read().execute(
            "WITH RECURSIVE ancestry(id) AS ("
            "  SELECT ?"
            "  UNION"
            "  SELECT e.parent_id FROM edge e "
            "  JOIN ancestry a ON e.child_id = a.id"
            ") "
            "SELECT child_id, parent_id FROM edge "
            "WHERE child_id IN (SELECT id FROM ancestry) "
            "ORDER BY child_id, ordinal",
            (node_id,),
        ).fetchall()
        parents: Dict[str, List[str]] = {}
        for row in rows:
            parents.setdefault(row["child_id"], []).append(row["parent_id"])

        # Iterative post-order DFS over parents: a node is emitted only
        # after all of its parents, so the result is oldest-first. Done
        # iteratively so deep lineages cannot exhaust the recursion limit.
        order: List[str] = []
        done: Set[str] = set()
        on_stack: Set[str] = set()  # ancestors currently being walked
        stack: List[Tuple[str, bool]] = [(node_id, False)]
        while stack:
            current, expanded = stack.pop()
            if current in done:
                continue
            if expanded:
                on_stack.discard(current)
                done.add(current)
                order.append(current)
                continue
            if current in on_stack:
                raise IntegrityError(
                    f"Cycle detected in lineage at node {current!r}; the "
                    "store's edges are corrupt."
                )
            on_stack.add(current)
            stack.append((current, True))  # emit after its parents
            for parent_id in parents.get(current, []):
                if parent_id not in done:
                    stack.append((parent_id, False))
        return order
