# Flutter ↔ Employer API

Same backend/API for Android and iOS. Every employer endpoint requires a Bearer token from an account with role
`employer` **and** a registered company; employers only ever see their own jobs and the applicants to them
(anything else → 404). A suspended employer keeps read access but cannot write (`403 employer_suspended`).

## 1. Registration — `POST /employers/register`
Any signed-in job seeker can register a company; the account role becomes `employer`
(withdraw active SharkHire applications first). Admins cannot register.
```dart
await http.post(Uri.parse('$baseUrl/employers/register'), headers: await authHeaders(), body: jsonEncode({
  'company_name': 'Acme Pvt Ltd', 'contact_email': 'hr@acme.pk', 'website': 'https://acme.pk',
  'industry': 'Software', 'company_size': '11-50', 'headquarters': 'Karachi', 'country': 'PK',
  'contact_phone': '+92 21 1234567', 'social_links': {'linkedin': 'https://linkedin.com/company/acme'}}));
```
`company_size`: `1-10, 11-50, 51-200, 201-500, 501-1000, 1000+`; social keys: linkedin, facebook, twitter, x,
instagram, youtube, github. After registering, **refresh your local user state** (`GET /auth/me`) to pick up the new role.

## 2. Company profile
`GET /employers/me`, `PUT /employers/me` (same body), `POST /employers/me/logo` (multipart `file`),
`DELETE /employers/me/logo`. Public page (no private contact data): `GET /employers/{id}`.

## 3. Verification workflow
`GET /employers/me/verification` → `{verification_status, can_submit, latest_request}`.
`POST /employers/me/verification` with at least one of `business_registration_number`, `company_website`,
`official_email` (+ `additional_information`). Status flow:
`unverified → pending → under_review → verified | rejected` (rejected can resubmit; the admin's `decision_reason` is shown).
Nothing is auto-verified. No identity documents are collected.

## 4. Job posting lifecycle
| Method | Path |
|---|---|
| POST | `/employer/jobs` (`submit_for_review: true` to submit immediately) |
| GET | `/employer/jobs?status=` |
| GET / PUT | `/employer/jobs/{id}` |
| PATCH | `/employer/jobs/{id}/status` `{"status": "...", "reason": "..."}` |
| DELETE | `/employer/jobs/{id}` (draft without applications → deleted, otherwise archived) |

Job body:
```json
{"title": "Backend Python Developer", "description": "≥30 chars…", "required_skills": ["Python", "FastAPI"],
 "preferred_skills": ["Docker"], "experience_level": "mid", "min_experience_years": 2, "max_experience_years": 5,
 "employment_type": "FULLTIME", "workplace_type": "onsite|remote|hybrid", "city": "Karachi", "country": "PK",
 "min_salary": 150000, "max_salary": 250000, "salary_currency": "PKR", "salary_period": "MONTH",
 "education_requirements": "BS CS", "application_deadline": "2026-12-31", "application_instructions": "…",
 "external_application_url": "https://…", "openings": 2, "benefits": ["Health insurance"]}
```
Statuses: `draft, pending_review, published, paused, closed, rejected, expired, archived, suspended`.
Employer transitions: draft → pending_review | archived · pending_review → draft · rejected → draft | pending_review |
archived · published → paused | closed · paused → published | closed · closed → archived · expired → pending_review |
closed | archived. Publishing requires a **verified** company and a future deadline. With moderation enabled
(default) an admin approves `pending_review` jobs; editing a published job sends it back to review.
`moderation_note` shows the admin's reason. Jobs past their deadline become `expired` and disappear from job seekers.
`suspended` is set only by admins.

## 5. Applicants
| Method | Path |
|---|---|
| GET | `/employer/jobs/{job_id}/applicants?status=&sort=newest|oldest` |
| GET | `/employer/applications?job_id=&status=` |
| GET | `/employer/applications/{id}` |
| PATCH | `/employer/applications/{id}/status` `{"status": "under_review", "notes": "private"}` |
| GET | `/employer/applications/{id}/cv` → `{url, expires_in, filename}` |

Employer review statuses: `submitted, under_review, shortlisted, interview, offered, hired, rejected`.
This pipeline is separate from the candidate's private tracker; candidates never see `employer_notes`, and employers
never see the candidate's notes/reminders. `candidate_withdrawn: true` → updates return `409 candidate_withdrawn`.
CV access returns a ~5-minute signed URL for the CV attached to that application only, and each access is audited.
```dart
final r = await http.get(Uri.parse('$baseUrl/employer/applications/$appId/cv'), headers: await authHeaders());
await launchUrl(Uri.parse(jsonDecode(r.body)['data']['url']), mode: LaunchMode.externalApplication);
```

## 6. Statistics — `GET /employer/statistics`
`total_jobs, jobs_by_status, total_applicants, applicants_by_status, applicants_per_job, pending_applications,
shortlisted, interviews, offers, hires, withdrawn` — computed from your own records only.

## 7. Reporting abuse (any user) — `POST /reports`
`{"target_type": "job|employer", "target_id": "uuid", "reason": "fraud|scam|misleading|inappropriate|discrimination|spam|other", "description": "..."}`;
`GET /reports/mine`. One open report per target per user (`409 duplicate_report`), 20/hour.
