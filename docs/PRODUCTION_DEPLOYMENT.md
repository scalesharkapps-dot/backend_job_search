# Production Deployment Guide

Nothing has been deployed. This guide prepares a deployment; steps marked 🔑 need company-owned
credentials or administrator access.

## 1. Required infrastructure
- A container host with HTTPS termination (e.g. Render, Fly.io, Railway, AWS ECS/App Runner, Google Cloud Run, a VM
  with Caddy/Nginx). The image listens on `$PORT` (8000) over HTTP; TLS is terminated by the platform/proxy.
- 🔑 A Supabase project (PostgreSQL + Auth + Storage).
- 🔑 A RapidAPI subscription to JSearch.
- A scheduler for maintenance jobs (platform cron, GitHub Actions schedule, or system cron).

## 2. Python version
3.11 (image base `python:3.11-slim`).

## 3. Environment variables
See [ENVIRONMENT_CONFIGURATION.md](ENVIRONMENT_CONFIGURATION.md). Production **must** set `APP_ENV=production`,
`DATABASE_URL`, `APP_SECRET_KEY` (≥32 random chars), `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `STORAGE_BACKEND=supabase`,
`SUPABASE_SERVICE_ROLE_KEY`, `JSEARCH_API_KEY`, `LOG_JSON=true`. The container refuses to start (exit 1, clear list
of problems) when these are missing (`python -m app.preflight`).

**Auth e-mail flows (required for forgot-password and verification links; the app still starts without them):**

| Variable | Value | Used by |
|---|---|---|
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` | every Supabase Auth call |
| `SUPABASE_ANON_KEY` | the project's anon/publishable key | signup, login, resend, **forgot-password** (`/auth/v1/recover`) |
| `SUPABASE_SERVICE_ROLE_KEY` | secret service-role key | Storage, admin ban/unban, account deletion (not forgot-password) |
| `AUTH_PASSWORD_RESET_REDIRECT_URL` | `sharkhire://auth/reset-password` | `redirect_to` of every recovery e-mail |
| `AUTH_EMAIL_REDIRECT_URL` | `sharkhire://auth/callback` | `redirect_to` of verification e-mails |

Enter values without quotes. When `AUTH_PASSWORD_RESET_REDIRECT_URL` is missing, startup logs
`Auth e-mail configuration: AUTH_PASSWORD_RESET_REDIRECT_URL is not set …`, and every forgot-password call returns
`503 password_reset_not_configured` with an ERROR log line naming the variable. It never falls back to localhost.

## 4. Supabase project 🔑
1. **Auth → Providers**: enable Email. Decide whether email confirmation is required.
2. **Auth → URL Configuration**: add the app deep links (`sharkhire://auth/callback`, `sharkhire://auth/reset-password`,
   or `sharkhire://**`) to Redirect URLs; set the same values in `AUTH_EMAIL_REDIRECT_URL` / `AUTH_PASSWORD_RESET_REDIRECT_URL`.
   Set **Site URL** to a real URL, not `http://localhost:3000`: Supabase uses it when a redirect is not allowed.
   **Auth → Emails → SMTP**: configure custom SMTP for production. The built-in mailer sends only a few e-mails per
   hour (`429 RATE_LIMITED`) and only to project team members (`503 email_delivery_unavailable`).
3. **JWT signing keys**: prefer asymmetric (ES256/RS256) keys — the API reads the JWKS automatically. The current
   SharkHire project signs with **ES256** (verified 2026-11-16). Only legacy HS256 projects need `SUPABASE_JWT_SECRET`
   **and** `HS256` in `SUPABASE_JWT_ALGORITHMS`.
4. **Connect → Session pooler**: copy that URI (IPv4, port 5432, `postgresql://postgres.<ref>:…@aws-…pooler.supabase.com`)
   and add `?sslmode=require`. The direct host `db.<ref>.supabase.co` is IPv6-only and fails on most IPv4 hosts.
   With the transaction pooler (6543) also set `DB_USE_PGBOUNCER=true`.
5. **Data API**: **intentionally disabled** on the SharkHire project — SharkHire never uses PostgREST, so `/rest/v1`
   errors (e.g. `PGRST002`) are expected; do not enable it or change Exposed schemas. Migration 0007 still enables
   RLS (no policies) and revokes `anon`/`authenticated` privileges as defence in depth. See [SUPABASE_SETUP.md](SUPABASE_SETUP.md).

## 5. Database migrations
Run before starting new app versions (one-off job using the same image):
```bash
docker run --rm --env-file prod.env sharkhire-api:<tag> alembic upgrade head
```
Take a backup first ([DATABASE_BACKUP_AND_RECOVERY.md](DATABASE_BACKUP_AND_RECOVERY.md)). Migrations 0001–0008 were
verified (upgrade → `alembic check` → downgrade → upgrade) on PostgreSQL 16 locally, and **applied to the SharkHire
Supabase database on 2026-11-16 (0008 the same day)** (PostgreSQL 17.11 via the Session pooler; `alembic check` clean). Verify any
database with `python scripts/verify_supabase_db.py` (structure, RLS, API-role privileges, CRUD with its own rows).
Never run the pytest suite with `TEST_DATABASE_URL` pointing at Supabase — it drops and recreates all tables.
Recommended: enable **Database → SSL configuration → Enforce SSL** (the pooler currently also accepts non-TLS clients). The app tables live in `public`; Supabase's `auth` schema is never modified.

## 6. Supabase Storage 🔑
Status: both buckets were **created on the SharkHire project on 2026-11-16** (private; `cvs` 5 MB PDF/DOCX,
`media` 2 MB JPEG/PNG/WebP) and verified end-to-end. For a new project: create two **private** buckets: `cvs` and `media` (names configurable). Do not make them public. The API uploads with
the service-role key and serves files only through short-lived signed URLs (`SIGNED_URL_TTL_SECONDS`, default 300).
Recommended bucket limits: `cvs` 5 MB, `application/pdf` + DOCX MIME; `media` 2 MB, image/jpeg|png|webp.

## 7. JSearch 🔑
Subscribe on RapidAPI, set `JSEARCH_API_KEY`. Default endpoint `/search-v2` (cursor pagination); legacy `/search`
supported via `JSEARCH_SEARCH_PATH=/search`. Tune `JSEARCH_GLOBAL_CALLS_PER_MINUTE` to your plan (it is per process;
total = value × processes).

**Techmap (second provider, optional):** subscribe to "Daily International Job Postings" (Techmap) on RapidAPI and
set `TECHMAP_API_KEY` (and `TECHMAP_API_HOST=daily-international-job-postings.p.rapidapi.com`). `GET /api/v1/jobs/search`
then queries both providers concurrently, merges and de-duplicates them (each item has `source` `jsearch` or
`techmap`; `pagination.providers` shows per-provider status), and still answers when one provider fails.
Cost control: each Techmap request is billed against the plan (Basic: 100 requests/month) and overage is
**billed, not blocked**. The backend caches Techmap results for `TECHMAP_CACHE_TTL_SECONDS` (6 h), fetches one
Techmap page (10 jobs) per search page, and stops calling when RapidAPI reports `TECHMAP_QUOTA_RESERVE` or fewer
requests left. Techmap is skipped for filters it cannot apply (experience, radius, publisher exclusion,
contractor/intern types). Leave the key empty to run JSearch only. Run `alembic upgrade head` (migration 0012
allows the `techmap` source).

## 8. Build the image
```bash
docker build -t sharkhire-api:1.0.0 .
```
Multi-stage, slim base, non-root user `app`, no `.env`/tests/docs/uploads in the image (`.dockerignore`).

## 9. Start the container
```bash
docker run -d --name sharkhire-api --env-file prod.env -p 8000:8000 -e WEB_CONCURRENCY=2 sharkhire-api:1.0.0
```
Workers: start with 2 per CPU core-ish; recommendations are CPU-bound. Graceful shutdown on SIGTERM (20 s).
No `--reload` in production.

## 10. HTTPS & domain 🔑
Point the domain to the platform/proxy, enable TLS (Let's Encrypt/managed certs). Behind a proxy set
`TRUST_PROXY_HEADERS=true` (client IPs for rate limiting) — only if the proxy overwrites `X-Forwarded-For`.
Set `PUBLIC_BASE_URL=https://<domain>`. CORS is irrelevant for native apps; leave `CORS_ORIGINS` empty unless a web
client exists.

## 11. Health checks
- Liveness: `GET /health` (process up; used by the Docker `HEALTHCHECK`).
- Readiness: `GET /api/v1/health/database` (200 only if `SELECT 1` succeeds, else 503).
- Auth e-mail configuration: `GET /api/v1/health/auth`. Returns 200 `{"status": "ok", "password_reset": "configured", …}`,
  or 503 `misconfigured` naming the missing variable (never its value). It does not check Supabase Dashboard settings.

## 12. Logging
`LOG_JSON=true` emits one JSON line per event with timestamp, level, request_id, method, path, status, duration.
Query strings, tokens, passwords and keys are never logged (redaction filter). Ship stdout to your platform's log
service.

## 13. Monitoring 🔑
Not configured (no credentials/subscription). Recommended: uptime check on `/api/v1/health/database`; alert on 5xx
rate and p95 latency from logs; optional Sentry (`sentry-sdk[fastapi]`) — add only with a DSN.

## 14. Backups
Supabase daily backups (plan-dependent) + PITR on paid plans. See the backup guide.

## 15. Rollback
1. Redeploy the previous image tag (keep the last 3 tags).
2. If the failed release ran a migration, run `alembic downgrade <previous_revision>` **only** after confirming the
   downgrade does not drop data you need — otherwise restore from backup.
3. Verify `/api/v1/health/database` and a smoke test (login, search, recommendations).

## Scheduled maintenance
Daily: `python -m app.scripts.maintenance` (expires SharkHire jobs past their deadline, purges old search cache).
