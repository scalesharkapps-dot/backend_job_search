# SharkHire API — Conventions & Reference

The same REST API serves the Android and iOS Flutter apps (one backend, one contract).
Full endpoint list: [API_ENDPOINTS.md](API_ENDPOINTS.md). Live schema: `GET /openapi.json`,
Swagger UI `/docs`, ReDoc `/redoc` (disable with `ENABLE_DOCS=false` if desired).

## Base URL & versioning
- Production: `https://<your-api-domain>/api/v1` (HTTPS only; the domain is not configured yet).
- Local: `http://localhost:8000/api/v1` (Android emulator: `http://10.0.2.2:8000/api/v1`).
- Health checks are unversioned-friendly: `GET /health`, `GET /api/v1/health`, `GET /api/v1/health/database`.

## Authentication
Send the Supabase access token on every protected call:
```
Authorization: Bearer <access_token>
```
Tokens come from `POST /auth/login` / `POST /auth/signup` / `POST /auth/refresh`.
The backend verifies signature, expiry, issuer, audience and algorithm on every request and
derives the user id from the token `sub` — client-supplied user ids are never trusted.
Roles (`job_seeker`, `employer`, `admin`) are stored server-side; tokens/bodies cannot change them.

## Response envelope
Success:
```json
{"success": true, "message": "Request completed successfully", "data": { }, "error": null}
```
Error:
```json
{"success": false, "message": "Invalid email or password.", "data": null,
 "error": {"code": "invalid_credentials", "message": "Invalid email or password.",
           "details": null, "request_id": "4f1c…"}}
```
Health endpoints return plain JSON (no envelope) for load balancers.
Every response carries `X-Request-ID` (send your own `X-Request-ID` to correlate logs).

## Status codes
| Code | Meaning | Typical `error.code` |
|---|---|---|
| 200/201 | OK / created | — |
| 400 | Bad request / business rule | `status_unchanged`, `self_action_forbidden` |
| 401 | Missing/invalid/expired token | `not_authenticated`, `invalid_token`, `token_expired` |
| 403 | Role or account state forbids | `insufficient_role`, `account_suspended`, `employer_not_verified` |
| 404 | Not found **or not yours** (no IDOR leaks) | `*_not_found` |
| 409 | Duplicate / conflicting state | `already_saved`, `duplicate_application`, `invalid_transition` |
| 413 | Body/file too large | `payload_too_large`, `file_too_large` |
| 415 | Unsupported/spoofed file type | `unsupported_file_type`, `invalid_file_signature` |
| 422 | Validation error (`details` lists fields) | `validation_error` |
| 429 | Rate limited (`Retry-After` header) | `rate_limited`, `job_search_rate_limited` |
| 502/503 | Upstream (Supabase/JSearch/DB) problem | `job_search_unavailable`, `auth_unavailable` |

Validation `details` format: `[{"field": "email", "message": "...", "type": "value_error"}]`.

## Pagination
List endpoints: `?page=1&page_size=20` →
```json
"data": {"items": [...], "pagination": {"page": 1, "page_size": 20, "total": 57, "has_more": true}}
```
Job search (JSearch) is **cursor-based**: pass `pagination.next_cursor` back as `cursor`;
`total_results` is always `null` (the provider does not report reliable totals).

## Dates
All timestamps are ISO-8601 UTC (`2026-11-15T10:00:00Z`). Interview responses also include `local_time`
in the interview's IANA timezone. Dates without time (deadlines) are `YYYY-MM-DD`.

## File uploads
`multipart/form-data` with field `file`. CVs: PDF/DOCX ≤ `CV_MAX_FILE_SIZE_MB` (5 MB).
Images (profile photo, company logo): JPEG/PNG/WebP ≤ 2 MB. File downloads are short-lived signed URLs
(`expires_in` seconds) — never cache them.

## Rate limits (defaults, per client IP/user, per server process)
Auth 10/min · verification/recovery emails 5/h · uploads 10/min · job search 20/min ·
recommendations 20/min · writes 60/min · reports 20/h. Respect `Retry-After`.

## Roles summary
| Area | job_seeker | employer | admin |
|---|---|---|---|
| Auth, profile, CV, search, recommendations, saved jobs, own applications | ✓ | ✓ (profile/search) | ✓ |
| Apply to SharkHire jobs | ✓ | ✗ | ✗ |
| Company profile, job postings, applicants | ✗ | ✓ (own only) | via admin API |
| Admin API (`/admin/*`) | ✗ | ✗ | ✓ |
