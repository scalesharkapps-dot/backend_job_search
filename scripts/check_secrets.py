"""Fail if likely secrets are committed (lightweight pre-commit / CI guard).

Scans tracked text files for JWTs, private keys, RapidAPI-style keys, Supabase
service keys and credentials embedded in database URLs. Test fixtures that use
obvious placeholders are allowed; mark intentional fakes with ``# secret-scan: ignore``.
"""

from __future__ import annotations

import pathlib
import re
import sys

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "db url with password": re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:(?!localtestpw@|PASSWORD@|password@|\[|<)[^@\s]{6,}@"),
    "rapidapi key": re.compile(r"(?i)rapidapi[-_ ]?key\s*[:=]\s*['\"]?[0-9a-f]{40,}"),
    "supabase secret key": re.compile(r"sb_secret_[A-Za-z0-9_-]{20,}"),
}
SKIP_DIRS = {".venv", ".git", "storage", "__pycache__", ".pytest_cache", ".ruff_cache"}
SUFFIXES = {".py", ".md", ".txt", ".ini", ".toml", ".yml", ".yaml", ".json", ".env", ".example", ".cfg", ""}


def main() -> int:
    findings = []
    for path in pathlib.Path(".").rglob("*"):
        if SKIP_DIRS & set(path.parts) or not path.is_file() or path.suffix not in SUFFIXES or path.name == ".env":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                if "secret-scan: ignore" in text.splitlines()[line - 1]:
                    continue
                findings.append(f"{path}:{line}: possible {name}")
    for f in findings:
        print(f)
    print(f"{len(findings)} potential secret(s) found")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
