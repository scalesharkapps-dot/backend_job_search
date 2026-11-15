"""Generate docs/API_ENDPOINTS.md from the application's OpenAPI schema.

    python scripts/export_endpoints.py
"""

from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, ".")
from app.main import create_app

PUBLIC = {
    ("GET", "/health"), ("GET", "/api/v1/health"), ("GET", "/api/v1/health/database"),
    ("POST", "/api/v1/auth/signup"), ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/resend-verification"), ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"), ("GET", "/api/v1/jobs/filters"),
}


def access(method: str, path: str) -> str:
    if (method, path) in PUBLIC:
        return "Public" + (" (recovery token)" if path.endswith("reset-password") else "")
    if path.startswith("/api/v1/admin"):
        return "Admin"
    if path.startswith("/api/v1/employer/") or (path.startswith("/api/v1/employers/me")):
        return "Employer (with company profile)"
    if path == "/api/v1/employers/register":
        return "Authenticated (job seeker → becomes employer)"
    return "Authenticated"


def main() -> None:
    spec = create_app().openapi()
    groups: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    count = 0
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            tag = (op.get("tags") or ["Other"])[0]
            groups[tag].append((method.upper(), path, access(method.upper(), path), op.get("summary", "")))
            count += 1
    lines = [
        "# SharkHire API — Endpoint Inventory",
        "",
        f"Generated from the OpenAPI schema by `scripts/export_endpoints.py`. **{count} operations.**",
        "",
        "Access: *Public* = no token; *Authenticated* = `Authorization: Bearer <Supabase access token>` "
        "(any active role); *Employer* / *Admin* = server-side role required. Suspended accounts receive 403.",
        "Interactive docs: `/docs` (Swagger) and `/redoc`; schema: `/openapi.json`.",
        "",
    ]
    for tag in sorted(groups):
        lines += [f"## {tag}", "", "| Method | Path | Access | Summary |", "|---|---|---|---|"]
        for method, path, acc, summary in sorted(groups[tag], key=lambda r: (r[1], r[0])):
            lines.append(f"| {method} | `{path}` | {acc} | {summary} |")
        lines.append("")
    with open("docs/API_ENDPOINTS.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    print(f"wrote docs/API_ENDPOINTS.md ({count} operations)")


if __name__ == "__main__":
    main()
