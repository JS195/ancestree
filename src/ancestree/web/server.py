"""The local live server: http.server on 127.0.0.1 + a SQL-backed JSON API.

The searchable explorer. The browser is a thin client: layout comes from
``/api/graph``, node details from ``/api/node/<id>``, and **search, diff
and the runs table are all answered by the server running SQL** through
the one query grammar (compiled onto ``store.find``) — no client-side
query engine exists anywhere (AD11).

Every endpoint resolves by database key (node_id, relpath) — never a
filesystem path — so path traversal is structurally impossible. The server
is deliberately single-threaded: requests serialize onto one SQLite read
connection, which a local single-user explorer never notices and which
keeps connection lifecycle trivial.

See REBUILD_BLUEPRINT.md section 5.3 (Phase 8, issue #19).
"""

from __future__ import annotations

import json
import mimetypes
import operator
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Set, Type
from urllib.parse import parse_qs, unquote, urlparse

from ..errors import AncestreeError
from .graph import build_graph, node_detail

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..store import LineageStore

_ASSETS = Path(__file__).parent / "assets"
_PAGE_PATH = _ASSETS / "live.html"
_VIS_JS_PATH = _ASSETS / "vis-network.min.js"
_VIS_MARKER = "__ANCESTREE_VIS_JS__"

# ---------------------------------------------------------------------------
# The query grammar (server-side; the ONE place queries are parsed)
#
# terms are whitespace-separated and ANDed:
#   key=value      equality (bools/numbers recognised), via store.find
#   key>v key<v    numeric comparisons (also >=, <=), via find predicates
#   anything-else  free text over node ids, step types and searchable values
# ---------------------------------------------------------------------------

_TERM = re.compile(r"^([A-Za-z_]\w*)(<=|>=|=|<|>)(.+)$")
_OPS: Dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
}


def _parse_value(text: str) -> Any:
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _numeric_predicate(op: str, bound: float) -> Callable[[Any], bool]:
    compare = _OPS[op]

    def predicate(stored: Any) -> bool:
        return (
            isinstance(stored, (int, float))
            and not isinstance(stored, bool)
            and compare(float(stored), bound)
        )

    return predicate


def search_ids(store: "LineageStore", query: str) -> List[str]:
    """The ids matching every term of `query`, oldest first. An empty
    query matches everything."""
    ordered = store._metadata.all_node_ids()
    keep = set(ordered)
    for term in query.split():
        match = _TERM.match(term)
        if match:
            key, op, value_text = match.groups()
            value = _parse_value(value_text)
            if op == "=":
                ids = store._metadata.find(**{key: value})
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                predicate = _numeric_predicate(op, float(value))
                ids = store._metadata.find(**{key: predicate})
            else:
                ids = []  # a range comparison needs a number
        else:
            like = f"%{term}%"
            rows = store.sql(
                "SELECT node_id FROM node "
                "WHERE node_id LIKE ? OR step_type LIKE ? "
                "UNION "
                "SELECT node_id FROM metadata "
                "WHERE searchable = 1 AND value LIKE ?",
                [like, like, like],
            )
            ids = [row["node_id"] for row in rows]
        keep &= set(ids)
        if not keep:
            break
    return [node_id for node_id in ordered if node_id in keep]


# ---------------------------------------------------------------------------
# Aggregate payloads (diff and the runs table — 0.1.x explorer parity)
# ---------------------------------------------------------------------------

_DIFF_STRUCTURAL = (
    "step_type",
    "generation",
    "healthy",
    "duration_s",
    "size_bytes",
    "created_display",
)


def diff_payload(store: "LineageStore", a: str, b: str) -> Dict[str, Any]:
    """An aligned two-node comparison: structural facts plus the union of
    both nodes' metadata keys, each row flagged same/different with a
    numeric delta where both sides are numbers."""
    detail_a = node_detail(store, a)
    detail_b = node_detail(store, b)

    def row(key: str, va: Any, vb: Any) -> Dict[str, Any]:
        delta = None
        numeric = (
            isinstance(va, (int, float))
            and isinstance(vb, (int, float))
            and not isinstance(va, bool)
            and not isinstance(vb, bool)
        )
        if numeric:
            delta = round(float(vb) - float(va), 6)
        return {"key": key, "a": va, "b": vb, "same": va == vb, "delta": delta}

    rows = [row(key, detail_a[key], detail_b[key]) for key in _DIFF_STRUCTURAL]
    meta_keys = sorted(set(detail_a["metadata"]) | set(detail_b["metadata"]))
    for key in meta_keys:
        value_a = detail_a["metadata"].get(key, {}).get("value")
        value_b = detail_b["metadata"].get(key, {}).get("value")
        rows.append(row(key, value_a, value_b))
    return {"a": a, "b": b, "rows": rows}


def runs_payload(store: "LineageStore") -> Dict[str, Any]:
    """The runs table: one row per node with its structural facts plus
    every numeric searchable metric — the "pick the best run" view."""
    metric_keys: Set[str] = set()
    rows: List[Dict[str, Any]] = []
    for node_id in store._metadata.all_node_ids():
        detail = node_detail(store, node_id)
        metrics: Dict[str, Any] = {}
        for key, envelope in detail["metadata"].items():
            value = envelope.get("value")
            if (
                envelope.get("searchable")
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                metrics[key] = value
                metric_keys.add(key)
        rows.append(
            {
                "node_id": node_id,
                "step_type": detail["step_type"],
                "created": detail["created_display"],
                "healthy": detail["healthy"],
                "duration_s": detail["duration_s"],
                "size_bytes": detail["size_bytes"],
                "metrics": metrics,
            }
        )
    return {"columns": sorted(metric_keys), "rows": rows}


# ---------------------------------------------------------------------------
# The HTTP server
# ---------------------------------------------------------------------------


@dataclass
class ServerHandle:
    """A running explorer: its URL and the means to stop it."""

    url: str
    _server: HTTPServer
    _thread: threading.Thread

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "ServerHandle":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _render_page() -> str:
    page = _PAGE_PATH.read_text(encoding="utf-8")
    if page.count(_VIS_MARKER) != 1:
        raise RuntimeError(
            f"Template marker {_VIS_MARKER!r} appears "
            f"{page.count(_VIS_MARKER)} times; live.html is out of sync "
            "with the server."
        )
    return page.replace(_VIS_MARKER, _VIS_JS_PATH.read_text(encoding="utf-8"))


def _make_handler(
    store: "LineageStore", page: str
) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass  # a local dev explorer, not a log source

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            try:
                self._route()
            except (KeyError, IndexError):
                self._send_json({"error": "missing required parameter"}, 400)
            except AncestreeError as exc:
                self._send_json({"error": str(exc)}, 404)
            except BrokenPipeError:  # client went away mid-response
                pass
            except Exception as exc:  # a bug should show up in the browser
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def _route(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            params = parse_qs(parsed.query)
            if path == "/":
                self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/graph":
                self._send_json(build_graph(store))
                return
            if path == "/api/runs":
                self._send_json(runs_payload(store))
                return
            if path == "/api/search":
                query = params.get("q", [""])[0]
                self._send_json({"ids": search_ids(store, query)})
                return
            if path == "/api/diff":
                self._send_json(
                    diff_payload(store, params["a"][0], params["b"][0])
                )
                return
            node_match = re.match(r"^/api/node/([^/]+)$", path)
            if node_match:
                self._send_json(node_detail(store, node_match.group(1)))
                return
            artifact_match = re.match(r"^/api/artifact/([^/]+)/(.+)$", path)
            if artifact_match:
                node_id, relpath = artifact_match.groups()
                # A pure database lookup: whatever the "path" says, it is
                # only ever a key — traversal cannot reach the filesystem.
                data = store._chunks.artifact_bytes(node_id, relpath)
                content_type = (
                    mimetypes.guess_type(relpath)[0]
                    or "application/octet-stream"
                )
                self._send(data, content_type)
                return
            self._send_json({"error": "not found"}, 404)

        def _send(
            self, body: bytes, content_type: str, status: int = 200
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
            self._send(
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

    return Handler


def start_server(store: "LineageStore", port: int = 0) -> ServerHandle:
    """Starts the explorer on 127.0.0.1 (an OS-assigned port when `port`
    is 0) and returns its handle. The caller stops it with
    ``handle.close()``; ``store.host_live_graph`` wraps this for the
    blocking Ctrl+C workflow."""
    server = HTTPServer(("127.0.0.1", port), _make_handler(store, _render_page()))
    thread = threading.Thread(
        target=server.serve_forever, name="ancestree-server", daemon=True
    )
    thread.start()
    host = str(server.server_address[0])
    real_port = int(server.server_address[1])
    return ServerHandle(
        url=f"http://{host}:{real_port}", _server=server, _thread=thread
    )
