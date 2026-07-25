"""The single-file, view-only static HTML export (AD8/AD11).

Renders the whole store into one shareable HTML file: the lineage graph
plus click-to-view node details — deliberately no search box, so the query
grammar exists exactly once (Python → SQL); the searchable explorer is the
live server.

Templating is by validated named markers (each must appear exactly once),
replacing 0.1.x's brittle exact-string tag matching. Because artifact
bytes now live in SQLite, referenced artifacts are made reachable at
export time: small images inline as data URIs (the file stays truly
single-file for the common case) and everything else materializes beside
the HTML under ``<name>_files/``.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 7, issue #18).
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .graph import build_graph, node_detail

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..store import LineageStore

_ASSETS = Path(__file__).parent / "assets"
_TEMPLATE_PATH = _ASSETS / "static.html"
_VIS_JS_PATH = _ASSETS / "vis-network.min.js"

_GRAPH_MARKER = "__ANCESTREE_GRAPH_JSON__"
_VIS_MARKER = "__ANCESTREE_VIS_JS__"

#: Images at or below this size inline as data URIs; anything larger (or
#: any non-image) materializes as a real file next to the export.
_INLINE_IMAGE_LIMIT = 256 * 1024

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def _substitute(template: str, marker: str, value: str) -> str:
    """Replaces one named marker, insisting it appears exactly once — a
    template drifting out of sync with the exporter fails loudly instead
    of shipping a silently broken file."""
    if template.count(marker) != 1:
        raise RuntimeError(
            f"Template marker {marker!r} appears "
            f"{template.count(marker)} times; static.html is out of sync "
            "with the exporter."
        )
    return template.replace(marker, value)


def export_static(
    store: LineageStore,
    dest: Path | str | None = None,
    include_artifacts: bool = True,
) -> Path:
    """Writes the view-only snapshot and returns its path.

    Args:
        store: The store to render.
        dest: Output path; defaults to ``<root>/interactive_pipeline.html``.
        include_artifacts: When True (default), every artifact becomes
            reachable from the file — small images as inline data URIs,
            the rest copied under ``<name>_files/``. Pass False for a
            metadata-only snapshot of a store with huge artifacts.
    """
    dest_path = (
        Path(dest) if dest is not None else store.root / "interactive_pipeline.html"
    )
    files_dir = dest_path.with_name(dest_path.stem + "_files")
    if include_artifacts and files_dir.exists():
        shutil.rmtree(files_dir)  # derived output from a previous export

    payload = build_graph(store)
    details: dict[str, Any] = {}
    for node in payload["nodes"]:
        node_id = node["id"]
        detail = node_detail(store, node_id)
        hrefs: dict[str, str] = {}
        if include_artifacts:
            for artifact in detail["artifacts"]:
                relpath = artifact["relpath"]
                hrefs[relpath] = _artifact_href(
                    store, files_dir, node_id, relpath, artifact["size"]
                )
        detail["artifact_hrefs"] = hrefs
        details[node_id] = detail

    graph = {
        "store": store.root.name,
        "nodes": payload["nodes"],
        "edges": payload["edges"],
        "details": details,
    }
    # "</" must never appear raw inside the inline JSON or the browser
    # would end the script element mid-payload; "<\/" is the same JSON.
    graph_json = json.dumps(graph).replace("</", "<\\/")

    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    html = _substitute(html, _VIS_MARKER, _VIS_JS_PATH.read_text(encoding="utf-8"))
    html = _substitute(html, _GRAPH_MARKER, graph_json)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(html, encoding="utf-8")
    return dest_path


def _artifact_href(
    store: LineageStore,
    files_dir: Path,
    node_id: str,
    relpath: str,
    size: int,
) -> str:
    """One artifact's reachable reference: a data URI for a small image,
    otherwise a relative link to a materialized copy."""
    suffix = ("." + relpath.rsplit(".", 1)[-1].lower()) if "." in relpath else ""
    data = store._chunks.artifact_bytes(node_id, relpath)
    if suffix in _IMAGE_MIME and size <= _INLINE_IMAGE_LIMIT:
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{_IMAGE_MIME[suffix]};base64,{encoded}"
    out = files_dir / node_id / relpath
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return f"{files_dir.name}/{node_id}/{relpath}"
