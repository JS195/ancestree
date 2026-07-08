"""Command-line interface: ``python -m ancestree <command> <root>``.

``serve`` hosts the searchable live explorer (this phase); ``export`` and
``compact`` complete the CLI in Phase 9. Stdlib argparse only (HC1).

See REBUILD_BLUEPRINT.md section 5.3 (Phase 8, issue #19).
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

    args = parser.parse_args(argv)

    from .store import LineageStore

    if args.command == "serve":
        store = LineageStore(args.root)
        try:
            store.host_live_graph(port=args.port)
        finally:
            store.close()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
