"""The lineage graph payload for the UI.

``build_graph`` returns the lightweight skeleton (ids, labels, step types,
levels, edges) that lays the graph out; ``node_detail`` returns one node's
full record (structural facts, provenance, metadata envelopes, artifact
list). ``explorer_graph`` combines them into the payload the classic
explorer template embeds as ``window.PIPELINE_DATA``: the skeleton plus,
per node, the complete ``entries`` envelope dict its client-side search,
details pane and runs table read.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 7, issue #18).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from ..errors import NodeNotFound
from ..util import format_timestamp, parse_iso_utc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..store import LineageStore


def assign_levels(
    node_ids: List[str], edges: List[Tuple[str, str]]
) -> Dict[str, int]:
    """Longest-path-from-a-root depth for every node (Kahn's algorithm):
    the hierarchical layout column each node renders in."""
    children: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
    indegree: Dict[str, int] = {node_id: 0 for node_id in node_ids}
    for parent, child in edges:
        children.setdefault(parent, []).append(child)
        indegree[child] = indegree.get(child, 0) + 1
        indegree.setdefault(parent, 0)

    level: Dict[str, int] = {node_id: 0 for node_id in indegree}
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    while ready:
        current = ready.pop()
        for child in children.get(current, []):
            level[child] = max(level[child], level[current] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return level


def build_graph(store: "LineageStore") -> Dict[str, Any]:
    """The layout skeleton: one entry per node (id, label, group, level,
    healthy) plus the parent→child edges, oldest node first."""
    records = store.find()
    edges = [
        (parent_id, record.node_id)
        for record in records
        for parent_id in record.parent_id
    ]
    levels = assign_levels([record.node_id for record in records], edges)
    nodes = [
        {
            "id": record.node_id,
            "label": f"{record.step_type}\n{record.node_id}",
            "group": record.step_type,
            "level": levels[record.node_id],
            "healthy": record.healthy,
        }
        for record in records
    ]
    return {
        "nodes": nodes,
        "edges": [{"from": parent, "to": child} for parent, child in edges],
    }


def explorer_graph(store: "LineageStore") -> Dict[str, Any]:
    """``build_graph``'s skeleton with each node carrying its full
    ``entries`` dict — the classic explorer's ``window.PIPELINE_DATA``
    payload, matching the 0.1.x envelope shape key for key."""
    payload = build_graph(store)
    for node in payload["nodes"]:
        node["entries"] = _node_entries(node_detail(store, node["id"]))
    return payload


def _node_entries(detail: Dict[str, Any]) -> Dict[str, Any]:
    """One node's ``entries`` envelopes, in the shape the template's JS
    reads: structural facts and provenance rebuilt as envelopes (the
    0.1.x store wrote them that way; the database keeps them as columns),
    user metadata as stored, and each artifact as a root-relative link
    the server resolves by database key."""

    def structural(value: Any) -> Dict[str, Any]:
        return {
            "value": value,
            "data_type": "text",
            "group": "Structural Properties",
            "searchable": True,
        }

    # The JS reads timestamps numerically (timeline, day filter) through
    # the attached epoch and shows the display string as the value.
    timestamp = structural(detail["created_display"])
    try:
        timestamp["epoch"] = parse_iso_utc(detail["created_utc"]).timestamp()
    except (TypeError, ValueError):
        pass

    entries = {
        "node_id": structural(detail["node_id"]),
        "parent_id": structural(detail["parent_id"]),
        "generation": structural(detail["generation"]),
        "step_type": structural(detail["step_type"]),
        "timestamp": timestamp,
        "healthy": structural(detail["healthy"]),
        "duration_s": structural(detail["duration_s"]),
        "size_bytes": structural(detail["size_bytes"]),
    }
    for key, value in detail["provenance"].items():
        entries[key] = {
            "value": value,
            "data_type": "text",
            "group": "Provenance",
            "searchable": False,
        }
    entries.update(detail["metadata"])
    for artifact in detail["artifacts"]:
        relpath = artifact["relpath"]
        entries[relpath] = {
            "value": f"{detail['node_id']}/{relpath}",
            "data_type": "link",
            "group": "Artifacts",
        }
    return entries


def node_detail(store: "LineageStore", node_id: str) -> Dict[str, Any]:
    """Everything the UI shows when a node is opened: structural facts,
    provenance, the metadata envelopes, and the artifact list (relpath +
    size; the exporter or live server decides how each becomes a link).

    Raises:
        NodeNotFound: If the id is not in the store.
    """
    node = store.get(node_id)
    if node is None:
        raise NodeNotFound(f"Node {node_id!r} not found in this store.")
    manifest = store._chunks.artifact_manifest(node.node_id)
    return {
        "node_id": node.node_id,
        "step_type": node.step_type,
        "generation": node.generation,
        "parent_id": list(node.parent_id),
        "created_utc": node.created_utc,
        "created_display": format_timestamp(node.created_utc),
        "healthy": node.healthy,
        "duration_s": node.duration_s,
        "size_bytes": node.size_bytes,
        "provenance": node.provenance,
        "metadata": node.metadata,
        "artifacts": [
            {"relpath": relpath, "size": record.size}
            for relpath, record in sorted(manifest.items())
        ],
    }
