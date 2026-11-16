# SharkHire Backend

FastAPI backend for **SharkHire**, a job-search mobile app (Flutter, Android + iOS). It provides Supabase-based
authentication, profiles and CV processing, JSearch (RapidAPI) job search for Pakistan and international markets,
an explainable job-recommendation engine, saved jobs / application tracking, and employer & admin APIs.

> Status and history: [PROGRESS.md](PROGRESS.md) · full project memory: [SHARKHIRE_PROJECT_MEMORY.md](SHARKHIRE_PROJECT_MEMORY.md)

## Technology stack
Python 3.11 · FastAPI · Uvicorn · Pydantic v2 / pydantic-settings · SQLAlchemy 2 (async) + psycopg 3 ·
Alembic · Supabase (PostgreSQL, Auth, Storage) · HTTPX · PyJWT · PyMuPDF · python-docx · Pillow · Pytest · Ruff.

## Architecture
```
Flutter (Android/iOS) ──HTTPS/JSON──▶ FastAPI (/api/v1)
                                        ├─ core/      config, DB, security (JWT/JWKS), errors, logging, middleware, rate limits, permissions
                                        ├─ api/v1/    thin routers (auth, profile, cv, jobs, recommendations, saved-jobs,
                                        │             applications, interviews, employers, employer, reports, admin)
                                        ├─ services/  business logic (supabase_auth, cv/, jsearch/, recommendations/,
                                        │             job_management, employer_service, admin_service, storage/, visibility, audit)
                                        ├─ models/    SQLAlchemy models (16 tables)   schemas/  Pydantic I/O models
                                        ▼
                     Supabase PostgreSQL · Supabase Auth · Supabase Storage · JSearch (RapidAPI)
```

### Supabase integration model
- FastAPI connects **directly to PostgreSQL** through the Supabase **Session pooler** (`DATABASE_URL`).
- **Supabase Auth** and **Supabase Storage** are separate integrations called by the backend; tokens are verified
  locally with ES256 via JWKS.
- The **Supabase Data API (PostgREST) is intentionally disabled** — `/rest/v1` errors such as `PGRST002` are expected
  and are not a backend fault. Flutter never queries the database or receives server credentials.
- RLS (migration 0007) is verified **through PostgreSQL** (`python scripts/verify_supabase_db.py`), not via REST.
  Details: [docs/SUPABASE_SETUP.md](docs/SUPABASE_SETUP.md).

## Project structure
```
app/            application package (main.py, server.py, preflight.py, core/, api/, models/, schemas/, services/, scripts/)
alembic/        migrations 0001–0008 (0007 = RLS + Data API lockdown, 0008 = no default API-role grants)
tests/          pytest suite (unit + mocked integration; opt-in real-service tests)
docs/           API, Flutter integration, deployment, security, backup guides
scripts/        load_test.py, export_endpoints.py, check_secrets.py, verify_supabase_db.py,
                e2e_supabase_audit.py, e2e_cleanup.py
Dockerfile      production image (non-root, preflight, healthcheck)
.github/workflows/ci.yml
```

## Local setup (Windows PowerShell)
```powershell
python -m venv .venv                    # already created in this folder
.venv\Scripts\Activate.ps1              # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env             # fill in values (never commit .env)
```
Command Prompt: `.venv\Scripts\activate.bat`. You can also skip activation and call `.venv\Scripts\python` directly.
The app boots in development without any credentials (health checks work; DB features need `DATABASE_URL`).

## Database & migrations
Set `DATABASE_URL` (Supabase: `postgresql://…:5432/postgres?sslmode=require`), then:
```powershell
.venv\Scripts\python -m alembic upgrade head        # apply
.venv\Scripts\python -m alembic current             # show revision
.venv\Scripts\python -m alembic downgrade -1        # revert one
.venv\Scripts\python -m alembic revision --autogenerate -m "message"
```
Override the target: `alembic -x db_url=postgresql://… upgrade head`.

## Run the API
```powershell
.venv\Scripts\python -m app.server --reload          # http://127.0.0.1:8000
```
(`app.server` selects the event loop psycopg needs on Windows.) Docs: `/docs`, `/redoc`, `/openapi.json`.
First admin: `python -m app.scripts.promote_admin --email you@company.com`.

## Docker
```bash
docker build -t sharkhire-api .
docker run --rm --env-file .env sharkhire-api alembic upgrade head
docker run -d -p 8000:8000 --env-file .env sharkhire-api
```
The container validates production configuration before starting and exits with a clear error if anything is missing.

## Tests & quality
```powershell
.venv\Scripts\python -m pytest                                   # SQLite, all external services mocked
$env:TEST_DATABASE_URL="postgresql://postgres:pw@localhost:55432/db"; .venv\Scripts\python -m pytest   # real PostgreSQL
.venv\Scripts\python -m pytest tests/test_real_integrations.py   # real Supabase/JSearch (needs REAL_* env vars)
.venv\Scripts\python -m ruff check app tests scripts
.venv\Scripts\python -m pip_audit -r requirements.txt
.venv\Scripts\python scripts/check_secrets.py
```
Local PostgreSQL for tests: `docker run -d --name sharkhire-pg-test -e POSTGRES_PASSWORD=localtestpw -e POSTGRES_DB=sharkhire_test -p 55432:5432 postgres:16-alpine`.

## Documentation
- **Start here (Flutter / handover): [docs/BACKEND_HANDOVER_REPORT.md](docs/BACKEND_HANDOVER_REPORT.md)**
- API: [docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md), [docs/API_ENDPOINTS.md](docs/API_ENDPOINTS.md)
- Flutter: [docs/FLUTTER_FINAL_BACKEND_INTEGRATION.md](docs/FLUTTER_FINAL_BACKEND_INTEGRATION.md) (index of all guides)
- Recommendations: [docs/RECOMMENDATION_SCORING.md](docs/RECOMMENDATION_SCORING.md)
- Admin: [docs/ADMIN_API_DOCUMENTATION.md](docs/ADMIN_API_DOCUMENTATION.md)
- Supabase: [docs/SUPABASE_SETUP.md](docs/SUPABASE_SETUP.md)
- Operations: [deployment](docs/PRODUCTION_DEPLOYMENT.md), [environment](docs/ENVIRONMENT_CONFIGURATION.md),
  [backups](docs/DATABASE_BACKUP_AND_RECOVERY.md), [security](docs/SECURITY_AND_OPERATIONS.md)

## Security notes
Never commit `.env` or real keys. The service-role key, JWT secret, RapidAPI key and DB URL are server-only and must
never be shipped in the Flutter app. See [docs/SECURITY_AND_OPERATIONS.md](docs/SECURITY_AND_OPERATIONS.md).
