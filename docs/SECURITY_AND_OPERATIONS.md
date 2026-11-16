# Security & Operations

This is an engineering security review with automated regression tests — **not** a formal penetration test.

## Authentication
- Supabase Auth issues tokens; the API verifies every token locally: signature (JWKS ES256/RS256, or HS256 only when a
  secret is configured), `exp` (10 s leeway), `iss`, `aud`, required `sub` (UUID), user-session `role`; `alg: none`,
  unknown `kid`, unsupported algorithms, malformed and oversized tokens are rejected. JWKS cached 10 min, refetched on
  unknown `kid` (throttled 30 s).
- Identity comes only from the verified `sub`; client user ids are never trusted.
- Logout revokes the Supabase session; access tokens stay valid until expiry (stateless JWT) — clients must delete them.
- Account suspension is checked on every request (DB flag) and optionally mirrored to Supabase (ban).
- Signup/recovery responses avoid account enumeration.

## Authorization
- Roles stored server-side (`users.role`); never read from tokens or bodies (unknown body fields → 422).
- Reusable dependencies: `require_role`, `get_current_user`, `get_employer_context` (`app/core/permissions.py`).
- Ownership enforced in queries (every lookup filtered by user/employer id) → foreign ids return 404 (no IDOR).
- Privilege escalation: only admins change roles; admins cannot change their own role/status; the last active admin is
  protected; the first admin is created by a server-side script.
- Employers see only applicants to their own jobs, only the CV attached to that application, via short-lived signed
  URLs, with audit logging. Candidate private notes are never exposed to employers and vice versa.

## Supabase Data API & Row Level Security
The Data API is **intentionally disabled** on the SharkHire project (FastAPI connects directly to PostgreSQL);
`/rest/v1` errors are expected. RLS is therefore verified **through PostgreSQL** (`scripts/verify_supabase_db.py`,
role switching to `anon`/`authenticated`), not over REST — see [SUPABASE_SETUP.md](SUPABASE_SETUP.md).
Background — if the Data API were enabled, Supabase would publish the `public` schema through PostgREST to the `anon`/`authenticated` roles, using keys that ship in
client apps. Without RLS, the app's tables would be readable/writable by anyone holding the publishable key (verified
locally: `anon` could read `users` before migration 0007). Migration **0007** enables RLS with no policies (deny-all)
and revokes all privileges from those roles on every SharkHire table; the backend connects as the table owner
(`postgres`), which bypasses RLS, and enforces authorization itself. `tests/test_foundation.py` fails if a new table
is not covered. If you ever connect the backend with a non-owner role, add explicit grants for that role.

## Input & files
- Pydantic v2 validation on all inputs; strict enums; URL fields allow only `http(s)` with a host (blocks
  `javascript:`, `data:`, credentials in URLs); LIKE wildcards escaped; SQLAlchemy parameterised queries only.
- Uploads: extension + MIME + magic-byte + structural checks; size caps while streaming; DOCX zip-bomb limits and macro
  rejection; images decoded and re-encoded (strips metadata; decompression-bomb limit); server-generated storage paths
  (path traversal impossible); private buckets + signed URLs (5 min).
- Request bodies limited by `Content-Length` check and a streaming ASGI limiter (`MAX_REQUEST_BODY_BYTES`, 12 MB).
- SSRF: the server only calls configured hosts (Supabase, RapidAPI); user-supplied URLs are stored, never fetched.

## Error handling & logging
- Central handlers return the standard envelope; no stack traces or upstream error bodies reach clients.
- Logs: request id, method, path (no query string), status, duration; redaction of bearer tokens, JWTs, passwords,
  API keys and DB passwords. Extracted CV text and contact data are not logged.
- Security headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.

## Rate limiting — limitation
Limits are in-memory **per process** (sliding window). With N workers/replicas the effective limit is N× and counters
reset on restart. This is not distributed rate limiting. For production at scale put a gateway/WAF limit in front
(Cloudflare, API Gateway) or add a shared store (Redis) — not implemented because no such infrastructure exists yet.
The JSearch outbound budget is also per process.

## Dependencies
`pip-audit` on 2026-11-15: **no known vulnerabilities** in runtime or dev requirements after upgrading Starlette,
FastAPI, PyJWT, python-multipart, Pillow and pytest (the initial pins had known CVEs). CI re-runs the audit.

## Secrets
`.env` git-ignored; `.env.example` contains no values; `scripts/check_secrets.py` scans for JWTs, private keys,
DB passwords, RapidAPI/Supabase keys (CI step).

## Operations runbook
| Symptom | Check |
|---|---|
| `/api/v1/health/database` 503 | DB credentials/SSL, Supabase status, pool exhaustion (`DB_POOL_SIZE`) |
| many `401 invalid_token` | wrong `SUPABASE_URL`/issuer/audience, signing key rotated (JWKS refetches automatically) |
| `503 job_search_unavailable` | RapidAPI key/subscription (401/403 logged as "authentication/subscription failure") |
| `429 job_search_rate_limited` | provider quota, quota reserve (`JSEARCH_QUOTA_RESERVE`, see `jsearch_quota` on `GET /admin/dashboard`) or `JSEARCH_GLOBAL_CALLS_PER_MINUTE` |
| `503 storage_unavailable` | Supabase Storage status, service-role key, bucket names |
| expired jobs still listed to employers | run `python -m app.scripts.maintenance` (also runs lazily) |
