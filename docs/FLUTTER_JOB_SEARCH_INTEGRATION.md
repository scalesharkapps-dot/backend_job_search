# Flutter ↔ Job Search (JSearch) Integration

1. **Base URL**: `https://<api-domain>/api/v1` — same for Android and iOS. Search requires a Bearer token
   (protects the RapidAPI quota); `/jobs/filters` is public.

2. **Search**: `GET /jobs/search`

3. **Query parameters**
| Param | Type | Notes |
|---|---|---|
| `query` | string (2–200) | required, e.g. `Python Developer` |
| `location` | string | e.g. `Karachi`, `London` |
| `city` | string | Optional. Only jobs in this city (Pakistan or international, e.g. `Karachi`, `Lahore`, `London`), exact city first then districts; spelling variants handled (`Karāchi`, `Bangalore`/`Bengaluru`). With `remote_jobs_only=true`, remote jobs without a city follow the city matches. Add `country` to disambiguate (`Hyderabad` + `pk`). A country name is rejected (422). Omit for the previous behaviour |
| `country` | ISO-2 | `pk`, `us`, `gb`… inferred from `location` when omitted; never defaults to Pakistan |
| `company` | string | applied locally (also added to the query text) |
| `remote_jobs_only` | bool | provider `work_from_home` |
| `employment_types` | repeatable | `FULLTIME`, `PARTTIME`, `CONTRACTOR`, `INTERN` |
| `experience` | repeatable | `no_experience`, `under_3_years_experience`, `more_than_3_years_experience`, `no_degree` |
| `date_posted` | enum | `all`, `today`, `3days`, `week`, `month` |
| `radius` | int km | |
| `language` | ISO-639-1 | |
| `exclude_job_publishers` | repeatable | |
| `min_salary` | number | applied locally (listings without salary are excluded when set) |
| `num_pages` | 1–3 | each provider page ≈ 10 jobs and costs one RapidAPI request |
| `cursor` | string | from the previous `pagination.next_cursor` |

4–6. **Examples**
```dart
Uri search(Map<String, dynamic> q) => Uri.parse('$baseUrl/jobs/search').replace(queryParameters: q);
// Pakistan
await http.get(search({'query': 'AI Engineer', 'location': 'Karachi'}), headers: await authHeaders());
// International
await http.get(search({'query': 'Backend Developer', 'country': 'gb'}), headers: await authHeaders());
// Remote + filters (repeatable params use lists)
await http.get(search({'query': 'Python Developer', 'remote_jobs_only': 'true',
    'employment_types': ['FULLTIME', 'CONTRACTOR'], 'date_posted': 'week'}), headers: await authHeaders());
```

7. **Employment filters**: see `GET /jobs/filters` (also lists which filters are provider vs local).

8. **Pagination (cursor)**
```dart
String? cursor;
Future<void> loadMore() async {
  final r = await http.get(search({'query': q, if (cursor != null) 'cursor': cursor}), headers: await authHeaders());
  final data = jsonDecode(r.body)['data'];
  jobs.addAll(data['items']);
  cursor = data['pagination']['next_cursor'];
  hasMore = data['pagination']['has_more'];
}
```
`total_results` is always `null` — show "Load more" rather than a page count.

9. **Job details**: `GET /jobs/{id}` → `data.job` with full description, `job_highlights`, `apply_options`.
Stale listings are refreshed from JSearch automatically (daily). `details_stale: true` means the refresh failed and the
stored copy is shown; `listing_unavailable: true` means the provider no longer lists the job.

10. **Response (summary item)**
```json
{"id": "uuid", "external_job_id": "abc==", "source": "jsearch", "job_title": "Python Developer",
 "employer_name": "TechCorp", "employer_logo": "https://…", "job_city": "Karachi", "job_country": "PK",
 "job_location": "Karachi, Sindh, PK", "job_is_remote": false, "job_employment_type": "FULLTIME",
 "job_min_salary": null, "job_max_salary": null, "job_salary_period": null, "job_currency": null,
 "job_posted_at": "2026-11-13T10:00:00Z", "job_apply_link": "https://…", "job_publisher": "LinkedIn",
 "job_description_snippet": "…", "job_required_skills": [], "is_external": true, "listing_unavailable": false}
```
`null` salary means *unknown*, not zero. Use the SharkHire `id` for saving, matching and applications.

11. **Errors**: `422` invalid params · `429 job_search_rate_limited` (wait `Retry-After`; also returned when the monthly
RapidAPI quota reserve is reached — cached results are still served as `stale`) ·
`503 job_search_unavailable` / `job_search_not_configured` · `502 job_search_bad_response`.

12. **Loading/empty states**: show a skeleton while loading; `items: []` with message "No jobs found for this search."
When `pagination.stale` is true show a banner "Showing saved results — live search is temporarily unavailable".

13. **Cache**: identical searches are served from cache for 1 hour (`pagination.cached: true`).

14. **Applying to external jobs**: open `job_apply_link` (or an entry of `apply_options`) with `url_launcher`
in the external browser. SharkHire does **not** submit external applications; afterwards offer
"Did you apply?" → `POST /applications {"job_id": "<id>"}` to track it.

**SharkHire-posted jobs** (from verified employers) are listed separately: `GET /jobs/sharkhire?q=&location=&remote=`.

## Opening an application link (Free plan: 5 per rolling 24 h)
Free users (no verified subscription, e.g. during the 3-day trial) receive `job_apply_link: null`, `apply_options: []`
and `apply_link_locked: true` in search, details, saved jobs and recommendations. Pro users (verified `app_access`
subscription) receive the links inline as before (`apply_link_locked: false`).

To apply, call `POST /api/v1/jobs/{job_id}/apply-link` with an `Idempotency-Key` header (8–128 chars, `A-Z a-z 0-9 . _ : -`,
new value per tap, the same value on retries):

* `200` → `data = {job_id, provider, apply_url, apply_options, already_opened, usage: {plan, unlimited, limit, used, remaining, reset_at}}`
  (`provider` is `jsearch` or `techmap`). The 5 are shared across both providers: 3 JSearch + 2 Techmap = 5 used.
  Re-opening a job already opened in the last 24 h is free (`already_opened: true`).
* `429` `DAILY_APPLICATION_LIMIT_REACHED` → message "You've reached your daily limit of 5 job applications. Try again after
  24 hours or upgrade to Pro for unlimited access.", `data = {code, limit, used, remaining: 0, reset_at, upgrade_available: true}`,
  `Retry-After` header (seconds).
* `400 internal_job_no_external_link` (SharkHire jobs: apply in-app), `404 apply_link_unavailable`, `403 subscription_required`.

`GET /api/v1/jobs/application-usage` returns the same `usage` object (for a "3 of 5 left today" indicator).
Search, job details, saving and tracking applications are never counted.
