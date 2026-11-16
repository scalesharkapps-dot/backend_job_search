# Environment Configuration

All configuration is read by `app/core/config.py` (Pydantic Settings) from environment variables or a local,
git-ignored `.env`. The complete annotated list is in [`.env.example`](../.env.example).

## Environments
| `APP_ENV` | Purpose | Notes |
|---|---|---|
| development | local work | boots without any external credentials; `STORAGE_BACKEND=local` |
| testing | pytest | set automatically by `tests/conftest.py` |
| staging | pre-production | behaves like production but not validated as strictly |
| production | live | startup validation (below) |

## Loading rules
- `DATABASE_URL` must start with `postgresql://` (or `postgres://`); otherwise startup fails with a clear message.
- Configuration errors are reported as `field: message` only — values (secrets) are never printed.
- `SHARKHIRE_ENV_FILE` overrides the dotenv path; empty disables it (the test suite does this so it never reads real
  credentials).

## Production startup validation (`Settings.validate_production`)
Startup fails if: `DATABASE_URL` missing or SQLite; `APP_SECRET_KEY` default/short; `SUPABASE_URL` or
`SUPABASE_ANON_KEY` missing; no JWT algorithms; `DEBUG=true`; `CORS_ORIGINS` contains `*`;
`STORAGE_BACKEND=local`; Supabase storage without `SUPABASE_SERVICE_ROLE_KEY`.

## Secrets — where they may live
| Variable | Server | Flutter app |
|---|---|---|
| `DATABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, `JSEARCH_API_KEY`, `APP_SECRET_KEY` | ✓ | **never** |
| `SUPABASE_ANON_KEY` | ✓ | not needed (the app uses the SharkHire API) |
Secrets are typed as `SecretStr` (never printed in reprs/logs).

## Groups
- **Database**: `DATABASE_URL` (`postgresql://…?sslmode=require`; normalised to the psycopg driver),
  pool settings, `DB_USE_PGBOUNCER` for the Supabase transaction pooler.
- **Auth / JWT**: `SUPABASE_JWT_ALGORITHMS` (HS256 accepted only when listed here AND `SUPABASE_JWT_SECRET` is set —
  a leftover legacy secret alone never enables it),
  `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER`, `SUPABASE_JWT_AUDIENCE`, `JWKS_CACHE_TTL_SECONDS`, `JWT_LEEWAY_SECONDS`.
- **Storage / uploads**: `STORAGE_BACKEND`, bucket names, `SIGNED_URL_TTL_SECONDS`, size limits.
- **JSearch**: key/host/base URL/paths, timeouts, retries, cache TTLs, global call budget.
- **Rate limits**: `RATE_LIMIT_*` (process-local).
- **Recommendations**: `REC_WEIGHT_*`, candidate/age limits, minimum score.
- **Moderation**: `JOB_MODERATION_ENABLED`.

## Local development (Windows)
```powershell
Copy-Item .env.example .env   # then edit
.venv\Scripts\python -m app.server --reload
```
`python -m app.server` selects the selector event loop needed by psycopg on Windows. With plain uvicorn on Windows use
`--loop app.core.eventloop:selector_loop_factory`.
