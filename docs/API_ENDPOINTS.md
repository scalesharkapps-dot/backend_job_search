# SharkHire API — Endpoint Inventory

Generated from the OpenAPI schema by `scripts/export_endpoints.py`. **95 operations.**

Access: *Public* = no token; *Authenticated* = `Authorization: Bearer <Supabase access token>` (any active role); *Employer* / *Admin* = server-side role required. Suspended accounts receive 403.
Interactive docs: `/docs` (Swagger) and `/redoc`; schema: `/openapi.json`.

## Admin

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/admin/audit-logs` | Admin | Administrative audit log |
| GET | `/api/v1/admin/dashboard` | Admin | Platform statistics |
| GET | `/api/v1/admin/employers` | Admin | List employers |
| GET | `/api/v1/admin/employers/{employer_id}` | Admin | Employer details |
| PATCH | `/api/v1/admin/employers/{employer_id}/status` | Admin | Suspend or reactivate an employer |
| PATCH | `/api/v1/admin/employers/{employer_id}/verification` | Admin | Review a verification request |
| GET | `/api/v1/admin/jobs` | Admin | List jobs for moderation |
| GET | `/api/v1/admin/jobs/{job_id}` | Admin | Job details (any status) |
| PATCH | `/api/v1/admin/jobs/{job_id}/moderation` | Admin | Approve or reject a pending job |
| PATCH | `/api/v1/admin/jobs/{job_id}/status` | Admin | Suspend, close or archive a job |
| GET | `/api/v1/admin/reports` | Admin | List abuse reports |
| GET | `/api/v1/admin/reports/{report_id}` | Admin | Report details |
| PATCH | `/api/v1/admin/reports/{report_id}/status` | Admin | Update a report |
| GET | `/api/v1/admin/users` | Admin | List users |
| GET | `/api/v1/admin/users/{user_id}` | Admin | User details |
| PATCH | `/api/v1/admin/users/{user_id}/role` | Admin | Change a user's role |
| PATCH | `/api/v1/admin/users/{user_id}/status` | Admin | Suspend or reactivate a user |

## Applications

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/applications` | Authenticated | List my applications |
| POST | `/api/v1/applications` | Authenticated | Create an application record |
| GET | `/api/v1/applications/reminders` | Authenticated | Upcoming deadlines, follow-ups and interviews |
| GET | `/api/v1/applications/statistics` | Authenticated | My application statistics |
| DELETE | `/api/v1/applications/{application_id}` | Authenticated | Delete an application record |
| GET | `/api/v1/applications/{application_id}` | Authenticated | Application details |
| PUT | `/api/v1/applications/{application_id}` | Authenticated | Update application details |
| GET | `/api/v1/applications/{application_id}/history` | Authenticated | Status history |
| GET | `/api/v1/applications/{application_id}/interviews` | Authenticated | List interviews |
| POST | `/api/v1/applications/{application_id}/interviews` | Authenticated | Record an interview |
| PATCH | `/api/v1/applications/{application_id}/status` | Authenticated | Change application status |

## Authentication

| Method | Path | Access | Summary |
|---|---|---|---|
| POST | `/api/v1/auth/forgot-password` | Public | Send a password-recovery email |
| POST | `/api/v1/auth/login` | Public | Sign in with email and password |
| POST | `/api/v1/auth/logout` | Authenticated | Revoke the current session |
| GET | `/api/v1/auth/me` | Authenticated | Current authenticated user |
| POST | `/api/v1/auth/refresh` | Public | Exchange a refresh token for new tokens |
| POST | `/api/v1/auth/resend-verification` | Public | Resend the signup confirmation email |
| POST | `/api/v1/auth/reset-password` | Public (recovery token) | Set a new password after recovery |
| POST | `/api/v1/auth/signup` | Public | Register with email and password |

## CV

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/cv` | Authenticated | List my CVs |
| POST | `/api/v1/cv/upload` | Authenticated | Upload a CV (PDF or DOCX) |
| DELETE | `/api/v1/cv/{cv_id}` | Authenticated | Delete a CV |
| GET | `/api/v1/cv/{cv_id}` | Authenticated | Get CV metadata |
| POST | `/api/v1/cv/{cv_id}/apply-to-profile` | Authenticated | Copy extracted CV data into my profile |
| GET | `/api/v1/cv/{cv_id}/download` | Authenticated | Get a short-lived download URL |
| GET | `/api/v1/cv/{cv_id}/extracted-data` | Authenticated | Get extracted CV data |
| PATCH | `/api/v1/cv/{cv_id}/extracted-data` | Authenticated | Review/correct extracted data (marks it user-confirmed) |
| POST | `/api/v1/cv/{cv_id}/primary` | Authenticated | Make this my primary CV |
| POST | `/api/v1/cv/{cv_id}/process` | Authenticated | Re-run CV processing |

## Employer Jobs & Applicants

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/employer/applications` | Employer (with company profile) | All applicants to my jobs |
| GET | `/api/v1/employer/applications/{application_id}` | Employer (with company profile) | One applicant |
| GET | `/api/v1/employer/applications/{application_id}/cv` | Employer (with company profile) | Applicant CV (signed URL) |
| PATCH | `/api/v1/employer/applications/{application_id}/status` | Employer (with company profile) | Update employer review status |
| GET | `/api/v1/employer/jobs` | Employer (with company profile) | My job postings |
| POST | `/api/v1/employer/jobs` | Employer (with company profile) | Create a job posting |
| DELETE | `/api/v1/employer/jobs/{job_id}` | Employer (with company profile) | Delete or archive a job |
| GET | `/api/v1/employer/jobs/{job_id}` | Employer (with company profile) | One of my job postings |
| PUT | `/api/v1/employer/jobs/{job_id}` | Employer (with company profile) | Update a job posting |
| GET | `/api/v1/employer/jobs/{job_id}/applicants` | Employer (with company profile) | Applicants for a job |
| PATCH | `/api/v1/employer/jobs/{job_id}/status` | Employer (with company profile) | Change job posting status |
| GET | `/api/v1/employer/statistics` | Employer (with company profile) | My hiring statistics |

## Employers

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/employers/me` | Employer (with company profile) | My company profile |
| PUT | `/api/v1/employers/me` | Employer (with company profile) | Update my company profile |
| DELETE | `/api/v1/employers/me/logo` | Employer (with company profile) | Remove company logo |
| POST | `/api/v1/employers/me/logo` | Employer (with company profile) | Upload company logo |
| GET | `/api/v1/employers/me/verification` | Employer (with company profile) | Verification status |
| POST | `/api/v1/employers/me/verification` | Employer (with company profile) | Submit (or resubmit) verification |
| POST | `/api/v1/employers/register` | Authenticated (job seeker → becomes employer) | Register a company (becomes an employer account) |
| GET | `/api/v1/employers/{employer_id}` | Authenticated | Public company profile |

## Health

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/health` | Public | API liveness |
| GET | `/api/v1/health/database` | Public | Database readiness |
| GET | `/health` | Public | Application liveness |

## Interviews

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/interviews/upcoming` | Authenticated | Upcoming interviews |
| DELETE | `/api/v1/interviews/{interview_id}` | Authenticated | Delete an interview |
| PUT | `/api/v1/interviews/{interview_id}` | Authenticated | Update an interview |

## Jobs

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/jobs/filters` | Public | Supported search filters |
| GET | `/api/v1/jobs/search` | Authenticated | Search jobs (Pakistan, international, remote) |
| GET | `/api/v1/jobs/sharkhire` | Authenticated | Jobs posted on SharkHire |
| GET | `/api/v1/jobs/suggestions` | Authenticated | Job title suggestions |
| GET | `/api/v1/jobs/{job_id}` | Authenticated | Job details |

## Profile

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/profile/me` | Authenticated | Get my profile |
| PATCH | `/api/v1/profile/me` | Authenticated | Partially update my profile |
| PUT | `/api/v1/profile/me` | Authenticated | Create or replace my profile |
| DELETE | `/api/v1/profile/me/photo` | Authenticated | Remove my profile photo |
| POST | `/api/v1/profile/me/photo` | Authenticated | Upload or replace my profile photo |

## Recommendations

| Method | Path | Access | Summary |
|---|---|---|---|
| POST | `/api/v1/recommendations/analyze` | Authenticated | Analyse a specific CV against jobs |
| GET | `/api/v1/recommendations/for-me` | Authenticated | Personalised recommendations |
| GET | `/api/v1/recommendations/history` | Authenticated | Previously generated recommendations |
| GET | `/api/v1/recommendations/jobs/{job_id}/match` | Authenticated | Match one job against my CV |
| GET | `/api/v1/recommendations/preferences` | Authenticated | Get recommendation preferences |
| PUT | `/api/v1/recommendations/preferences` | Authenticated | Replace recommendation preferences |

## Reports

| Method | Path | Access | Summary |
|---|---|---|---|
| POST | `/api/v1/reports` | Authenticated | Report a suspicious job or employer |
| GET | `/api/v1/reports/mine` | Authenticated | My reports |

## Saved Jobs

| Method | Path | Access | Summary |
|---|---|---|---|
| GET | `/api/v1/saved-jobs` | Authenticated | List my saved jobs |
| GET | `/api/v1/saved-jobs/check/{job_id}` | Authenticated | Is this job saved? |
| DELETE | `/api/v1/saved-jobs/{job_id}` | Authenticated | Unsave a job |
| GET | `/api/v1/saved-jobs/{job_id}` | Authenticated | Get one saved job |
| POST | `/api/v1/saved-jobs/{job_id}` | Authenticated | Save a job |
