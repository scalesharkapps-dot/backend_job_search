# Database Backup & Recovery

No backups exist yet (no production database has been connected). This is the procedure to adopt.

## What to protect
All SharkHire data lives in the `public` schema of the Supabase PostgreSQL database (16 tables + `alembic_version`).
Uploaded files (CVs, photos, logos) live in Supabase Storage buckets and are **not** included in database backups.
User credentials live in Supabase Auth (`auth` schema), managed and backed up by Supabase.

## Supabase options
- **Daily automated backups**: available on paid plans (retention depends on plan). Check *Database → Backups*.
- **Point-in-time recovery (PITR)**: paid add-on; recommended for production (RPO of minutes).
- **Free plan**: no downloadable automated backups → schedule your own `pg_dump` (below).

## Manual / scheduled logical backup
```bash
# Use the direct or session-pooler connection string (not the transaction pooler).
pg_dump "$DATABASE_URL" --schema=public --format=custom --no-owner --file sharkhire_$(date +%F).dump
```
Recommendations: daily, plus before every migration; keep 7 daily + 4 weekly + 6 monthly; store encrypted
(e.g. object storage with server-side encryption, restricted IAM); never commit dumps (they contain personal data).

## Storage backups
Periodically copy bucket contents with the Supabase Storage API/CLI or S3-compatible tooling to a private,
encrypted location with the same retention as database backups.

## Verify backups (monthly)
1. Start a scratch PostgreSQL (`docker run -e POSTGRES_PASSWORD=… -p 55432:5432 postgres:16-alpine`).
2. `pg_restore --no-owner -d postgresql://postgres:…@localhost:55432/postgres sharkhire_YYYY-MM-DD.dump`
3. `alembic -x db_url=<scratch url> current` must show the expected head; row counts look plausible.
4. Point a local API at it and call `/api/v1/health/database`.

## Restore
1. Put the API in maintenance (scale to 0 or block traffic).
2. Restore into a **new** database/project first when possible; validate; then switch `DATABASE_URL`.
3. For in-place restore: `pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" dump`.
4. Run `alembic upgrade head` if the dump predates the current code.
5. Smoke test, then restore traffic.

## Migration recovery
- Every migration is reversible and was tested up/down on PostgreSQL 16 locally.
- If a migration fails midway, PostgreSQL rolls back the transaction (DDL is transactional); fix and re-run.
- If a deployed migration must be reverted: `alembic downgrade -1` — check the downgrade does not drop data you need
  (downgrades of 0002–0006 drop the tables/columns they added). Otherwise restore from the pre-migration backup.

## Data retention
Users can delete CVs (file + extracted data), application records, saved jobs. Admin audit logs and abuse reports are
retained for accountability. Define legal retention periods with the company before launch.
