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

## 4. Supabase project 🔑
1. **Auth → Providers**: enable Email. Decide whether email confirmation is required.
2. **Auth → URL Configuration**: add the app deep links (`sharkhire://auth/callback`, `sharkhire://auth/reset-password`)
   to Redirect URLs; set the same values in `AUTH_EMAIL_REDIRECT_URL` / `AUTH_PASSWORD_RESET_REDIRECT_URL`.
3. **JWT signing keys**: prefer asymmetric (ES256/RS256) keys — the API reads the JWKS automatically. For legacy HS256
   projects set `SUPABASE_JWT_SECRET` (server only).
4. **Database → Connection string**: use the session pooler or direct connection (port 5432) with `sslmode=require`.
   With the transaction pooler (6543) also set `DB_USE_PGBOUNCER=true`.

## 5. Database migrations
Run before starting new app versions (one-off job using the same image):
```bash
docker run --rm --env-file prod.env sharkhire-api:<tag> alembic upgrade head
```
Take a backup first ([DATABASE_BACKUP_AND_RECOVERY.md](DATABASE_BACKUP_AND_RECOVERY.md)). Migrations 0001–0006 were
verified (upgrade → `alembic check` → downgrade → upgrade) on PostgreSQL 16 locally — **not yet on the Supabase
instance**. The app tables live in `public`; Supabase's `auth` schema is never modified.

## 6. Supabase Storage 🔑
Create two **private** buckets: `cvs` and `media` (names configurable). Do not make them public. The API uploads with
the service-role key and serves files only through short-lived signed URLs (`SIGNED_URL_TTL_SECONDS`, default 300).
Recommended bucket limits: `cvs` 5 MB, `application/pdf` + DOCX MIME; `media` 2 MB, image/jpeg|png|webp.

## 7. JSearch 🔑
Subscribe on RapidAPI, set `JSEARCH_API_KEY`. Default endpoint `/search-v2` (cursor pagination); legacy `/search`
supported via `JSEARCH_SEARCH_PATH=/search`. Tune `JSEARCH_GLOBAL_CALLS_PER_MINUTE` to your plan (it is per process;
total = value × processes).

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
