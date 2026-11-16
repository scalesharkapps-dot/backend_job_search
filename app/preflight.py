"""Fail-fast configuration check run before the server starts (``python -m app.preflight``).

uvicorn's multi-worker supervisor restarts workers that crash on import, so a
misconfigured container would otherwise keep running without serving traffic.
"""

from __future__ import annotations

import sys

from app.core.config import SettingsError, get_settings


def main() -> int:
    try:
        problems = get_settings().validate_production()
    except SettingsError as exc:
        print(f"SharkHire {exc}", file=sys.stderr)
        return 1
    if problems:
        print("SharkHire configuration error:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Configuration OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
