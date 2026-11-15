"""Development/production launcher: ``python -m app.server``.

On Windows the default ProactorEventLoop is incompatible with psycopg's async
mode, so a selector-loop factory is passed to uvicorn (see app/core/eventloop.py). Linux/Docker deployments are unaffected.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SharkHire API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else args.workers,
        proxy_headers=True,
        server_header=False,
        loop="app.core.eventloop:selector_loop_factory" if sys.platform == "win32" else "auto",
    )


if __name__ == "__main__":
    main()
