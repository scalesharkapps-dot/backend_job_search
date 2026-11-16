"""Generate endpoint tables from the application's OpenAPI schema.

    python scripts/export_endpoints.py

Writes:
* docs/API_ENDPOINTS.md            — summary inventory (method, path, access, summary)
* docs/_endpoint_reference.md      — detailed table with path/query parameters and request-body
                                     fields, embedded into docs/BACKEND_HANDOVER_REPORT.md
"""

from __future__ import annotations

import sys
from collections import defaultdict
from typing import Any

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


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def body_fields(spec: dict[str, Any], op: dict[str, Any]) -> str:
    content = (op.get("requestBody") or {}).get("content") or {}
    if not content:
        return ""
    if "multipart/form-data" in content:
        schema = _resolve(spec, content["multipart/form-data"]["schema"])
        return "multipart: " + ", ".join(f"`{n}`*" for n in schema.get("properties", {}))
    schema = content.get("application/json", {}).get("schema", {})
    if "anyOf" in schema:  # optional body
        schema = next((s for s in schema["anyOf"] if "$ref" in s), schema)
    schema = _resolve(spec, schema)
    required = set(schema.get("required", []))
    props = list(schema.get("properties", {}))
    shown = [f"`{p}`" + ("*" if p in required else "") for p in props]
    return "JSON: " + ", ".join(shown) if shown else "JSON"


def params(op: dict[str, Any], where: str) -> str:
    out = []
    for p in op.get("parameters", []):
        if p.get("in") == where:
            out.append(f"`{p['name']}`" + ("*" if p.get("required") else ""))
    return ", ".join(out)


def main() -> None:
    spec = create_app().openapi()
    groups: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    count = 0
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            groups[(op.get("tags") or ["Other"])[0]].append((method.upper(), path, op))
            count += 1

    lines = [
        "# SharkHire API — Endpoint Inventory", "",
        f"Generated from the OpenAPI schema by `scripts/export_endpoints.py`. **{count} operations.**", "",
        "Access: *Public* = no token; *Authenticated* = `Authorization: Bearer <Supabase access token>` "
        "(any active role); *Employer* / *Admin* = server-side role required. Suspended accounts receive 403.",
        "Interactive docs: `/docs` (Swagger) and `/redoc`; schema: `/openapi.json`.", "",
    ]
    ref = [f"_Generated from OpenAPI — {count} operations. `*` = required. Bodies list the accepted fields; "
           "see `/docs` for types and limits._", ""]
    for tag in sorted(groups):
        rows = sorted(groups[tag], key=lambda r: (r[1], r[0]))
        lines += [f"## {tag}", "", "| Method | Path | Access | Summary |", "|---|---|---|---|"]
        ref += [f"### {tag}", "", "| Method | Path | Access | Path / query params | Body | Summary |", "|---|---|---|---|---|---|"]
        for method, path, op in rows:
            lines.append(f"| {method} | `{path}` | {access(method, path)} | {op.get('summary', '')} |")
            pq = "; ".join(x for x in (params(op, "path"), params(op, "query")) if x)
            ref.append(f"| {method} | `{path}` | {access(method, path)} | {pq} | {body_fields(spec, op)} | {op.get('summary', '')} |")
        lines.append("")
        ref.append("")
    with open("docs/API_ENDPOINTS.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    with open("docs/_endpoint_reference.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(ref))
    print(f"wrote docs/API_ENDPOINTS.md and docs/_endpoint_reference.md ({count} operations)")


if __name__ == "__main__":
    main()
