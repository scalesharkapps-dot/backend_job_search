# SharkHire — Backend Handover Report

**Date:** 2026-11-17 · **API version:** v1 · **Audience:** Flutter (Android + iOS) developers and the deployment owner.

> **Status in one line:** all 8 backend phases are implemented and verified against the real Supabase project
> (database, Auth, Storage) and the real JSearch API. The API is **ready for Flutter integration against a local or
> staging server**. It is **not yet production-ready**: it has not been deployed, and in-app **account deletion is
> missing** (an App Store / Google Play requirement) — see §8.

---

## 1. Audit results

| Area | Result | Evidence |
|---|---|---|
| Phase 1 — Foundation (config, health, logging, errors) | **PASS** | 95 operations; production preflight OK; standard envelope on every endpoint |
| Phase 2 — Authentication (Supabase Auth, ES256/JWKS) | **PASS** | real login/refresh/logout/ban; tampered/expired/forged tokens rejected |
| Phase 3 — Profile & CV (PDF/DOCX, Supabase Storage) | **PASS** | real uploads, extraction, signed downloads, cross-user denial |
| Phase 4 — JSearch job search | **PASS** | real `/search-v2`: Pakistan + remote, cursor pagination, cache, quota guard |
| Phase 5 — Recommendations | **PASS** | real jobs scored and explained (top match 94% in the captured example) |
| Phase 6 — Saved jobs, applications, interviews, reminders | **PASS** | status history, timezones, statistics, user isolation |
| Phase 7 — Employer & admin | **PASS** | verification, moderation, applicant CV access, suspension + Supabase ban, audit log |
| Phase 8 — Production readiness | **PARTIAL** | Docker image, CI workflow, docs, RLS lockdown done; **not deployed**; blockers in §8 |
| Supabase DB migrations | **PASS** | revision `0008` (head) applied; `alembic check` clean; RLS on all 17 tables |
| API response format / error handling | **PASS** | one envelope for success and errors; stable `error.code`; `request_id` on every error |
| CORS | **PASS for native apps** | no CORS needed for Android/iOS; Flutter **Web** would need `CORS_ORIGINS` |
| Environment & secrets | **PASS** | `.env` git-ignored; no secrets in code/responses/logs; service keys server-only |
| Automated tests | **PASS** | 385 passed, 0 failed, 2 skipped (the 2 real-service tests run separately and pass) |
| Live end-to-end audit | **PASS** | 57/57 checks (real DB + Auth + Storage); JSearch live checks 16/16 |

---

## 2. Connecting from Flutter

| Item | Value |
|---|---|
| Base URL (local dev) | `http://localhost:8000/api/v1` — Android emulator: `http://10.0.2.2:8000/api/v1`; a real phone: `http://<your PC's LAN IP>:8000/api/v1` and run the server with `--host 0.0.0.0` |
| Base URL (production) | `https://<api-domain>/api/v1` — **no domain yet** |
| Interactive docs | `/docs` (Swagger), `/redoc`, schema `/openapi.json` (can generate a Dart client) |
| Auth header | `Authorization: Bearer <access_token>` |
| Content type | `application/json`; uploads use `multipart/form-data` with field `file` |
| Same API for Android and iOS | yes — nothing platform-specific |

### Response envelope (every `/api/v1` endpoint)
```json
{ "success": true, "message": "Request completed successfully", "data": { }, "error": null }
```
```json
{ "success": false, "message": "CV not found.", "data": null,
  "error": { "code": "cv_not_found", "message": "CV not found.", "details": null, "request_id": "b299d8f9c9a8490f92db9d8100a7fd14" } }
```
Branch on **HTTP status + `error.code`**, show `error.message` to users, log `request_id`. Validation errors (`422`)
list fields in `error.details` (`[{"field": "email", "message": "...", "type": "value_error"}]`).

### Status codes
`200/201` OK · `400` business rule · `401` missing/invalid/expired token (`token_expired` → refresh) · `403` role or
suspended account · `404` not found **or not yours** · `409` duplicate/conflict · `413` too large · `415` bad file type ·
`422` validation · `429` rate limited (`Retry-After` header) · `502/503` upstream (Supabase / JSearch) unavailable.

### Pagination
Most lists: `?page=1&page_size=20` → `data.items` + `data.pagination {page, page_size, total, has_more}`.
Job search (JSearch) is **cursor-based**: pass `pagination.next_cursor` back as `cursor`; `total_results` is always `null`.

### Dates & files
Timestamps are ISO-8601 UTC. File URLs (`url`, `profile_photo_url`, `logo_url`) are signed and expire in ~5 minutes —
use immediately, never store.

### Recommended client setup (Dart, `dio` + `flutter_secure_storage`)
```dart
final storage = const FlutterSecureStorage();
final dio = Dio(BaseOptions(baseUrl: const String.fromEnvironment('API_BASE',
    defaultValue: 'http://10.0.2.2:8000/api/v1')));

dio.interceptors.add(InterceptorsWrapper(
  onRequest: (o, h) async {
    final t = await storage.read(key: 'access_token');
    if (t != null) o.headers['Authorization'] = 'Bearer $t';
    h.next(o);
  },
  onError: (e, h) async {
    final code = e.response?.data is Map ? e.response!.data['error']?['code'] : null;
    if (e.response?.statusCode == 401 && code == 'token_expired' && await refreshTokens()) {
      final o = e.requestOptions..headers['Authorization'] = 'Bearer ${await storage.read(key: 'access_token')}';
      return h.resolve(await dio.fetch(o));
    }
    h.next(e);
  },
));

Future<bool> refreshTokens() async {
  final rt = await storage.read(key: 'refresh_token');
  if (rt == null) return false;
  try {
    final r = await Dio(BaseOptions(baseUrl: dio.options.baseUrl)).post('/auth/refresh', data: {'refresh_token': rt});
    await storage.write(key: 'access_token', value: r.data['data']['access_token']);
    await storage.write(key: 'refresh_token', value: r.data['data']['refresh_token']); // single-use: always replace
    return true;
  } catch (_) { await storage.deleteAll(); return false; }
}
```
Role-based navigation after login: `data.user.role` = `job_seeker` | `employer` | `admin`. After employer registration
call `GET /auth/me` again (the role changes server-side).

---

## 3. Example requests and responses (captured from the running backend, tokens redacted)

### Login — `POST /api/v1/auth/login` (public)
```json
{ "email": "ayesha@example.com", "password": "S3cure-Passw0rd" }
```
```json
{ "success": true, "message": "Login successful",
  "data": { "access_token": "<access_token>", "refresh_token": "<refresh_token>", "token_type": "bearer",
            "expires_in": 3600, "expires_at": 1794865754,
            "user": { "id": "7d6737fe-763c-4be8-91e6-8e4c9278e98b", "email": "ayesha@example.com", "full_name": "Ayesha Khan",
                      "role": "job_seeker", "is_active": true, "email_verified": true, "created_at": "2026-11-16T20:49:14.627220Z" } },
  "error": null }
```
Signup (`POST /auth/signup`, `email`, `password`, optional `full_name`) returns `email_confirmation_required: true`
and no session — email confirmation is enabled on the project. Password: ≥8 chars, upper + lower case + digit.

### Update profile — `PUT /api/v1/profile/me` (Bearer)
```json
{ "full_name": "Ayesha Khan", "headline": "Python Developer", "city": "Islamabad", "country": "PK",
  "skills": ["python", "django", "postgres"], "current_job_title": "Python Developer",
  "years_of_experience": 3, "preferred_employment_types": ["FULLTIME"] }
```
Response `data` = the full profile; skills come back canonicalised (`["Python", "Django", "PostgreSQL"]`) with
`profile_completion` (0–100) and `completion_breakdown`. `PATCH` updates only the fields sent; `role`/`email` are rejected (422).

### Upload CV — `POST /api/v1/cv/upload` (Bearer, multipart `file`, PDF/DOCX ≤ 5 MB)
```json
{ "success": true, "message": "CV uploaded successfully",
  "data": { "id": "2b110d66-2de6-452a-a968-adc363f8d84f", "original_filename": "ayesha_cv.pdf", "file_type": "pdf",
            "file_size": 1219, "page_count": 1, "processing_status": "completed", "extraction_error": null,
            "is_primary": true, "processed_at": "2026-11-16T20:49:20.024694Z", "skills_found": 8,
            "created_at": "2026-11-16T20:49:19.710567Z", "updated_at": "2026-11-16T20:49:20.024694Z" },
  "error": null }
```
`processing_status`: `completed` | `no_text` | `ocr_required` (scanned PDF; OCR not supported) | `failed`.
Then `GET /cv/{id}/extracted-data` → `skills` (e.g. `["Python","FastAPI","Django","PostgreSQL","Machine Learning","Git","AWS","Docker"]`),
`work_experience`, `education`, `certifications`, `years_of_experience`; `GET /cv/{id}/download` → `{url, expires_in, filename}`.

### Search jobs — `GET /api/v1/jobs/search?query=Python%20Developer&location=Pakistan` (Bearer)
```json
{ "success": true, "message": "Jobs retrieved successfully",
  "data": { "items": [ { "id": "cf512f1d-417f-42ae-8751-c8da781cfddb", "external_job_id": "…", "source": "jsearch",
              "job_title": "Junior Python Developer – Karachi", "employer_name": "Workato", "employer_logo": null,
              "job_city": "Karachi", "job_state": "Sindh", "job_country": "PK", "job_location": "Sindh",
              "job_is_remote": false, "job_employment_type": "FULLTIME", "job_employment_types": ["FULLTIME"],
              "job_min_salary": null, "job_max_salary": null, "job_salary_period": null, "job_currency": null,
              "job_posted_at": "2026-11-06T00:00:00Z", "job_posted_at_timestamp": 1793923200,
              "job_apply_link": "https://pk.expertini.com/job/…", "job_publisher": "Pakistan Jobs Expertini",
              "job_description_snippet": "…", "job_required_skills": [], "is_external": true, "listing_unavailable": false,
              "last_seen_at": "2026-11-17T…Z" } ],
            "pagination": { "page_size": 10, "has_more": true, "next_cursor": "<opaque>", "total_results": null,
                            "source": "jsearch", "cached": false, "stale": false, "country": "pk" } },
  "error": null }
```
Remote: `?query=AI/ML%20Engineer&remote_jobs_only=true`. Filters: `employment_types` (repeatable), `experience`,
`date_posted`, `country`, `radius`, `company`/`min_salary` (applied locally). `null` salary = unknown, not zero.
External jobs are applied to by opening `job_apply_link` in the browser — SharkHire does not submit them.

### Recommendations — `GET /api/v1/recommendations/for-me?page_size=20` (Bearer)
```json
{ "success": true, "message": "Recommendations generated",
  "data": { "items": [ { "job": { "id": "cf512f1d-…", "job_title": "Junior Python Developer – Karachi", "…": "same fields as search" },
                         "match": { "match_percentage": 94, "score_confidence": 0.72, "confidence_label": "high",
                                    "matched_skills": ["Python"], "matched_inferred_skills": [],
                                    "missing_required_skills": [], "missing_preferred_skills": [],
                                    "experience_match": "meets", "title_match": "highly_relevant",
                                    "location_match": "same_country", "employment_match": "compatible",
                                    "reasons": ["The role closely matches your background as Python Developer.", "…"],
                                    "concerns": [], "relevance_gate": null, "score_breakdown": { },
                                    "disclaimer": "Compatibility score based on your profile data; not a prediction of hiring outcome." } } ],
            "pagination": { "page": 1, "page_size": 20, "total": 29, "has_more": true },
            "candidate": { "cv_id": "2b110d66-…", "skills_considered": 8, "data_sources": ["profile", "cv_extracted"],
                           "years_of_experience": 3.0, "missing_information": [] },
            "algorithm_version": "weighted-1.1" },
  "error": null }
```
`422 insufficient_profile` (with `details.missing`) when the user has neither skills nor job titles — prompt them to upload a CV.

### Save a job — `POST /api/v1/saved-jobs/{job_id}` (Bearer, no body) → `201`; again → `409 already_saved`.

### Track an application — `POST /api/v1/applications` (Bearer)
```json
{ "job_id": "cf512f1d-417f-42ae-8751-c8da781cfddb", "discovered_via": "recommendation", "deadline": "2026-12-15" }
```
Then `PATCH /api/v1/applications/{id}/status` with `{ "status": "shortlisted", "notes": "Recruiter called" }` →
`data.status = "shortlisted"`, `data.history = [{"previous_status": null, "new_status": "applied"}, {"previous_status": "applied", "new_status": "shortlisted"}]`,
`match_percentage: 94` (snapshot from recommendations). For a manual external job send `job_title` (+ `company_name`, `job_apply_url`) instead of `job_id`.
For **SharkHire employer jobs** (`source: "sharkhire"`) the same call *submits* the application: `selected_cv_id` is required, `cover_letter` optional.

### Errors
```json
{ "success": false, "message": "Authentication is required.", "data": null,
  "error": { "code": "not_authenticated", "message": "Authentication is required.", "details": null, "request_id": "…" } }
```
```json
{ "success": false, "message": "Your account role cannot access this resource.", "data": null,
  "error": { "code": "insufficient_role", "message": "Your account role cannot access this resource.", "details": null, "request_id": "…" } }
```

---

## 4. Authentication requirements & roles
* **Public:** health checks, `auth/signup`, `auth/login`, `auth/refresh`, `auth/resend-verification`, `auth/forgot-password`,
  `auth/reset-password` (needs a recovery token), `jobs/filters`. Everything else needs a Bearer token.
* Tokens are Supabase access tokens (ES256, 1 hour). The backend verifies signature, expiry, issuer, audience and algorithm
  on every request and derives the user from the token — client-supplied user ids are never trusted.
* Roles are stored server-side: `job_seeker` (default), `employer` (via `POST /employers/register`), `admin`
  (server script only). Employers see only their own jobs/applicants; admins use `/admin/*`.
* Logout revokes the session in Supabase; also delete stored tokens on the device.
* Change password while signed in: `POST /auth/reset-password` with the Bearer token and `{ "new_password": "…" }`.
* Deep links: `sharkhire://auth/callback` (email confirmation) and `sharkhire://auth/reset-password` — register them in
  AndroidManifest / iOS URL types; they are already configured in `.env`.

---

## 5. All available API endpoints

{{ENDPOINTS}}

---

## 6. CORS & networking
* Native Android/iOS apps do not use CORS; requests without an `Origin` header are served normally (verified).
* `CORS_ORIGINS` is empty, so **browser** origins are refused (verified: preflight → 405). Set `CORS_ORIGINS=https://your-web-app`
  only if a Flutter **Web** build or another web client is added. Never use `*` (production startup refuses it).
* Production must be HTTPS (Android blocks cleartext by default; allow `10.0.2.2` / LAN IP only in debug builds).

## 7. Configuration & security (verified)
* `.env` is git-ignored and never committed; `.env.example` documents every setting (no values).
* Server-only secrets: `DATABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `JSEARCH_API_KEY`, `APP_SECRET_KEY` — never ship them in
  the Flutter app. The app needs **no Supabase keys at all** (it calls only this API).
* Database: Supabase Session pooler, TLS (`sslmode=require`), migrations at `0008`; RLS on every table with no policies and no
  privileges for `anon`/`authenticated`; the Supabase Data API is intentionally disabled (`/rest/v1` errors are expected).
* Production startup validation (`python -m app.preflight`) passes with the current values when `APP_ENV=production`.
* Rate limits per client: auth 10/min, emails 5/h, uploads 10/min, search 20/min, recommendations 20/min, writes 60/min.
* JSearch plan: **200 requests/month**; the backend stops calling at 5 remaining (`429` + cached results), visible as
  `jsearch_quota` on `GET /admin/dashboard`.

## 8. Missing functionality, blockers and remaining production requirements

### Blockers before an app-store release
| # | Item | Why |
|---|---|---|
| B1 | **In-app account deletion** (`DELETE` own account: remove Supabase user, CVs/files, profile, applications) | Required by Apple App Store guideline 5.1.1(v) and Google Play's account-deletion policy for apps that create accounts. **Not implemented.** |
| B2 | **Deployment**: hosting, HTTPS domain, production `.env` (`APP_ENV=production`, `LOG_JSON=true`, `TRUST_PROXY_HEADERS=true` behind a proxy) | The API has only run locally. The Docker image and guide are ready (`docs/PRODUCTION_DEPLOYMENT.md`). |

### Should do before launch
| # | Item |
|---|---|
| S1 | Create the first admin: the person signs in once, then `python -m app.scripts.promote_admin --email <email>` |
| S2 | Supabase: enable **Database → SSL configuration → Enforce SSL** (pooler still accepts non-TLS clients) |
| S3 | Reset the Supabase database password if not done (it appeared in a local session transcript earlier) |
| S4 | Check the GitHub Actions CI run, then commit the uncommitted audit work |
| S5 | Decide on `ENABLE_DOCS` for production (Swagger is public when enabled; no secrets are exposed by it) |
| S6 | Raise `JSEARCH_CACHE_TTL_SECONDS` or upgrade the RapidAPI plan — 200 requests/month is small for real users |
| S7 | Rate limiting is per server process; use a gateway/WAF limit if running several instances |

### Not provided by the backend (decide if needed)
Push notifications / device-token registration (FCM/APNs) and an in-app notifications list — reminders are data only ·
job alerts / saved searches · change email · social login (Google/Apple) · OCR for scanned CVs · filtering SharkHire
jobs by employer on the company page (only an `open_jobs` count is returned).

---

## 9. Where to find more
`docs/FLUTTER_FINAL_BACKEND_INTEGRATION.md` (index of all Flutter guides) · `docs/API_DOCUMENTATION.md` (conventions) ·
`docs/API_ENDPOINTS.md` (generated inventory) · `docs/SUPABASE_SETUP.md` · `docs/PRODUCTION_DEPLOYMENT.md` ·
`docs/SECURITY_AND_OPERATIONS.md` · `docs/RECOMMENDATION_SCORING.md`.
Regenerate the endpoint table after API changes: `python scripts/export_endpoints.py` then rebuild this report
(`python scripts/build_handover_report.py`).
