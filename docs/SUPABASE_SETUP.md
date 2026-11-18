# Supabase Setup & Integration Model

SharkHire uses Supabase as **three separate integrations**, all reached only by the FastAPI backend:

| Supabase service | How SharkHire uses it | Credential (server-side only) |
|---|---|---|
| **PostgreSQL** | Direct connection from FastAPI (SQLAlchemy async + psycopg) and Alembic via the **Session pooler** | `DATABASE_URL` |
| **Auth** | Sign-up/login/refresh/logout/recovery via the Auth REST API; access tokens verified locally with **ES256 via JWKS** | `SUPABASE_URL`, `SUPABASE_ANON_KEY` (publishable), `SUPABASE_SERVICE_ROLE_KEY` (admin ban/unban only) |
| **Storage** | Private buckets `cvs` and `media`; uploads and short-lived signed URLs from the backend | `SUPABASE_SERVICE_ROLE_KEY` |

The Flutter apps talk **only** to the FastAPI API. They never receive the database URL/password, the secret
(service-role) key, or the JWT signing configuration, and they never query the database directly.

## Data API (PostgREST) — intentionally DISABLED
The Supabase Data API is **disabled on purpose** in the SharkHire project. SharkHire does not use PostgREST,
GraphQL or direct client database access. Consequently:

* `/rest/v1/...` requests return errors (e.g. `503 PGRST002` or `401` from the gateway) — **this is expected**.
* Do **not** enable the Data API, change *Exposed schemas* or restart the project to "fix" these errors.
* Health of SharkHire is measured with `GET /health` and `GET /api/v1/health/database`, not with `/rest/v1`.

Defence in depth (in case the Data API is ever re-enabled): migration **0007** enables Row Level Security with no
policies on all SharkHire tables and revokes all privileges from `anon` / `authenticated`; migration **0008** stops
new tables from being auto-granted to those roles.

## Database connection
Use **Connect → Session pooler** (IPv4):
```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```
The direct host `db.<ref>.supabase.co` is IPv6-only and does not work on IPv4-only networks. Recommended: enable
**Database → SSL configuration → Enforce SSL** (the pooler currently also accepts non-TLS clients; SharkHire itself
always uses `sslmode=require`).

## RLS & privileges — verified through PostgreSQL (not REST)
Because the Data API is disabled, security is verified directly in the database:
```bash
python scripts/verify_supabase_db.py
```
or manually:
```sql
SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;  -- all true (17 tables)
SELECT * FROM pg_policies WHERE schemaname = 'public';                                        -- none (deny-all)
SELECT grantee, count(*) FROM information_schema.role_table_grants
 WHERE table_schema = 'public' AND grantee IN ('anon','authenticated','service_role') GROUP BY grantee;
```
Verified state (2026-11-16): RLS on all 17 tables (16 app tables + `alembic_version`), 0 policies, owner `postgres`;
`anon`/`authenticated`: 0 grants, SELECT denied when switching to those roles; `service_role`: full grants +
BYPASSRLS (Supabase default, backend-only); backend role `postgres`: BYPASSRLS, so the API is unaffected.

**Important:** RLS here is a *lockdown*, not the authorization model. Per-user isolation (own profile, CVs,
applications; employer-only applicants; admin-only APIs) is enforced in FastAPI and verified by the automated
suite and the end-to-end audit (cross-user requests return 404/403).

### Future tables
Supabase's default privileges used to grant `anon`/`authenticated` full rights on every new `public` table and
sequence created by `postgres`. Three layers now protect future tables:
1. **Migration 0008** (applied 2026-11-16) revokes those default privileges for role `postgres` in `public`
   (tables + sequences) — verified: a probe table created afterwards had **no** API-role grants.
   Defaults owned by `supabase_admin` (platform-managed, only for objects Supabase itself creates) and default
   function privileges are intentionally left unchanged.
2. The project's **automatic RLS** event trigger `ensure_rls` (`public.rls_auto_enable()`, fires on `CREATE TABLE`)
   enables RLS immediately — verified on a probe table.
3. A CI test fails unless every model table is listed in the RLS lockdown migration. `rls_auto_enable()` is executable by `anon`/`authenticated`, but PostgreSQL
refuses direct calls to event-trigger functions (verified), so this grant is not exploitable.

## Storage
Buckets (created 2026-11-16): `cvs` private, 5 MB, PDF/DOCX only; `media` private, 2 MB, JPEG/PNG/WebP only.
Objects are stored under server-generated paths (`cvs/<user-uuid>/<random>.pdf`) and served via signed URLs
(`SIGNED_URL_TTL_SECONDS`, default 300).

### Storage configuration checklist (CV uploads)
CV uploads fail with `503 storage_not_configured` ("File uploads aren't available right now because the server's
file storage isn't set up") whenever the **running server** has `STORAGE_BACKEND=supabase` but no
`SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY`. A local `.env` is never shipped (git- and docker-ignored), so these
must be set in the hosting provider's environment variables:

| Variable | Value |
|---|---|
| `STORAGE_BACKEND` | `supabase` |
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Dashboard > Project Settings > API Keys > **secret** key (`sb_secret_...`) or legacy `service_role` JWT. **Not** the publishable/anon key (that gives `storage_auth_failed`). Server-side only. |
| `SUPABASE_STORAGE_CV_BUCKET` | `cvs` (existing bucket). Any private bucket name works, e.g. `user-cvs`, but changing it on a live system orphans files already stored in the old bucket. |
| `SUPABASE_STORAGE_MEDIA_BUCKET` | `media` |
| `CV_MAX_FILE_SIZE_MB` | `5` |

Create / verify buckets (never modifies or empties existing buckets):
```bash
python scripts/setup_storage.py              # read-only check
python scripts/setup_storage.py --create     # create missing buckets as PRIVATE (5 MB PDF/DOCX; 2 MB images)
python scripts/setup_storage.py --roundtrip  # upload/sign/download/delete one test object
```
Manual alternative: Dashboard > Storage > New bucket > name `cvs`, **Public bucket OFF**, file size limit 5 MB,
allowed MIME types `application/pdf, application/vnd.openxmlformats-officedocument.wordprocessingml.document`.
No storage RLS policies are needed (and none should be added for `anon`/`authenticated`): only the backend accesses
objects, with the secret key; users get time-limited signed URLs for their own files only.

On a deployed server: `GET /api/v1/health/storage` returns `200 {"storage": "connected"}` or `503` with
`storage_not_configured | storage_bucket_missing | storage_auth_failed | storage_bucket_public | storage_unavailable`.
The server also logs an ERROR at startup naming (never printing) any missing variable.

## Verification tools
| Command | What it checks | Writes |
|---|---|---|
| `python scripts/verify_supabase_db.py` | connectivity, TLS, revision, schema vs models, RLS, API-role privileges, CRUD/FK/rollback | own marked rows, removed |
| `python scripts/e2e_supabase_audit.py <api> <db_url>` | all 8 phases over HTTP with real Auth/Storage | throwaway users/rows/files, removed by `scripts/e2e_cleanup.py` |
| `python scripts/setup_storage.py [--create] [--roundtrip]` | storage key, buckets exist and are private, optional round-trip | `--create`: missing buckets; `--roundtrip`: one test object, removed |
| `python -m pytest` | full suite (SQLite / local PostgreSQL) | **never point `TEST_DATABASE_URL` at Supabase** — it drops tables |
