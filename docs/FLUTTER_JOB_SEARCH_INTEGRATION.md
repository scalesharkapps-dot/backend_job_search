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
