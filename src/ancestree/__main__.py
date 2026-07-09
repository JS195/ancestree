"""Command-line interface: ``python -m ancestree <command> <root>``.

``serve`` hosts the searchable live explorer, ``export`` writes grep-able
meta.json sidecars, ``compact`` reclaims space. Stdlib argparse only (HC1).

See REBUILD_BLUEPRINT.md section 5.3.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ancestree",
        description="Exploratory pipeline tracking on a single SQLite store.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve", help="host the searchable live explorer for a store"
    )
    serve.add_argument("root", type=Path, help="the store's root directory")
    serve.add_argument(
        "--port",
        type=int,
        default=0,
        help="port to bind on 127.0.0.1 (default: OS-assigned)",
    )

    export = commands.add_parser(
        "export",
        help="write grep-able meta.json sidecars for every node",
    )
    export.add_argument("root", type=Path, help="the store's root directory")
    export.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="output directory (default: <root>/export)",
    )

    compact = commands.add_parser(
        "compact",
        help="reclaim space: drop unreferenced chunks and shrink the database",
    )
    compact.add_argument("root", type=Path, help="the store's root directory")

    args = parser.parse_args(argv)

    from .store import LineageStore

    store = LineageStore(args.root)
    try:
        if args.command == "serve":
            # The CLI has no cell to return to: block until Ctrl+C.
            store.host_live_graph(port=args.port, block=True)
            return 0
        if args.command == "export":
            dest = store.export(dest=args.dest)
            count = len(store.find())
            print(f"wrote sidecars for {count} node(s) to {dest}")
            return 0
        if args.command == "compact":
            removed = store.compact()
            print(f"removed {removed} unreferenced chunk(s)")
            return 0
    finally:
        store.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
