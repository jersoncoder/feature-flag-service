"""Run the server: ``python -m flagservice.server`` or ``flagservice-server``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from flagservice.server.app import create_app
from flagservice.server.settings import Settings


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="flagservice-server",
        description="Serve the feature-flag API. Settings come from FLAGS_ADMIN_KEY and "
        "FLAGS_DATABASE.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        parser.exit(2, f"flagservice-server: {exc}\n")
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
