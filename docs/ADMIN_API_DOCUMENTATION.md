# Admin API

All `/api/v1/admin/*` endpoints require an active `admin` account (403 for others, 401 without token).
There is **no admin web panel**; these APIs are for a future admin client or tooling.
Every mutating action writes an `admin_audit_logs` row in the same transaction (actor, action, target,
previous/new values, reason, request id). Secrets and tokens are never stored in the audit log.

## Bootstrapping the first admin
No API can grant admin to oneself. On the server (with `DATABASE_URL` set), after the person has signed in once:
```bash
python -m app.scripts.promote_admin --email admin@company.com --reason "initial admin"
```
Recorded in the audit log as `user.role_change.bootstrap` with no actor.

## Dashboard — `GET /admin/dashboard`
`total_users, active_users, suspended_users, job_seekers, employers, admins, employer_profiles, verified_employers,
pending_verification_requests, suspended_employers, sharkhire_jobs_total, published_jobs, pending_moderation,
closed_jobs, external_jobs_stored, total_applications, internal_applications, pending_reports` + `definitions`.

## Users
| Method | Path | Body |
|---|---|---|
| GET | `/admin/users?q=&role=&is_active=&page=` | |
| GET | `/admin/users/{id}` | |
| PATCH | `/admin/users/{id}/status` | `{"is_active": false, "reason": "..."}` |
| PATCH | `/admin/users/{id}/role` | `{"role": "employer", "reason": "..."}` |
Rules: no self status/role change (`400 self_action_forbidden`); the last active admin cannot be suspended or demoted
(`409 last_admin`). Suspension blocks the user's API access immediately (checked on every request). When
`SUPABASE_SERVICE_ROLE_KEY` is configured the Supabase account is also banned/unbanned (`auth_provider_updated: true`);
otherwise only the SharkHire-level block applies (the user could still obtain Supabase tokens, but every API call is rejected).

## Employers
| Method | Path | Body |
|---|---|---|
| GET | `/admin/employers?verification_status=&account_status=&q=` | |
| GET | `/admin/employers/{id}` | includes verification request history |
| PATCH | `/admin/employers/{id}/verification` | `{"decision": "under_review|approve|reject", "reason": "..."}` (reason required for reject) |
| PATCH | `/admin/employers/{id}/status` | `{"account_status": "active|suspended", "reason": "..."}` |
Suspending an employer hides all their listings from job seekers instantly and blocks their write operations.

## Job moderation
| Method | Path | Body |
|---|---|---|
| GET | `/admin/jobs?status=&source=&employer_id=&q=` | |
| GET | `/admin/jobs/{id}` | any status |
| PATCH | `/admin/jobs/{id}/moderation` | `{"decision": "approve|reject", "reason": "..."}` — only `pending_review` jobs; approval requires a verified, active employer |
| PATCH | `/admin/jobs/{id}/status` | `{"status": "suspended|paused|closed|archived|published", "reason": "..."}` — `published` only reinstates a suspended job |
Employers cannot override suspensions or rejections.

## Reports
| Method | Path | Body |
|---|---|---|
| GET | `/admin/reports?status=&target_type=` | |
| GET | `/admin/reports/{id}` | shows `reporter_id` (no reporter contact details) |
| PATCH | `/admin/reports/{id}/status` | `{"status": "under_review|resolved|rejected|pending", "resolution_note": "..."}` (note required to resolve/reject) |
Take enforcement actions (suspend job/employer) with the endpoints above, then resolve the report.

## Audit log — `GET /admin/audit-logs?action=&actor_id=&target_type=&target_id=`
Actions include `employer.register, employer.verification.{under_review,approve,reject}, employer.suspend,
employer.reactivate, employer.cv_access, job.status_change, job.archived, job.resubmitted_after_edit,
job.moderation.{approve,reject}, job.admin_status_change, user.suspend, user.reactivate, user.role_change,
user.role_change.bootstrap, report.status_change`. Audit rows cannot be modified through the API.
