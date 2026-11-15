# Flutter ↔ Job Recommendations

Same API for Android and iOS; Bearer token required. Algorithm details: [RECOMMENDATION_SCORING.md](RECOMMENDATION_SCORING.md).

## 1. Personalised list — `GET /recommendations/for-me`
Params: `cv_id` (optional, defaults to primary CV), `page`, `page_size` (≤50), `min_score` (0–100), `remote_only`.
```dart
final r = await http.get(Uri.parse('$baseUrl/recommendations/for-me?page=1&page_size=20'), headers: await authHeaders());
final data = jsonDecode(r.body)['data'];
for (final item in data['items']) {
  final job = item['job'];     // same shape as job search summaries
  final match = item['match']; // see below
}
```
`data.candidate` tells what was used: `cv_id`, `skills_considered`, `data_sources`, `missing_information`.

## 2. Single job — `GET /recommendations/jobs/{job_id}/match?cv_id=`
Use on the job details screen.

## 3. CV selection — `POST /recommendations/analyze`
```dart
await http.post(Uri.parse('$baseUrl/recommendations/analyze'), headers: await authHeaders(),
    body: jsonEncode({'cv_id': cvId, 'page': 1, 'page_size': 20}));
```
`422 cv_not_processed` if that CV wasn't processed successfully; `404` if not yours.

## 4–7. Match object
```json
{"match_percentage": 87, "score_confidence": 0.85, "confidence_label": "high",
 "matched_skills": ["Python", "FastAPI"], "matched_inferred_skills": ["SQL"],
 "missing_required_skills": ["Docker"], "missing_preferred_skills": ["AWS"],
 "job_skills_source": "explicit", "experience_match": "meets", "title_match": "highly_relevant",
 "location_match": "compatible", "employment_match": "compatible", "relevance_gate": null,
 "score_breakdown": {"required_skills": {"score": 75, "weight": 40, "applicable": true, "label": "strong", "details": {}}},
 "reasons": ["Your skills match the job's requirements: Python and FastAPI."],
 "concerns": ["Required skills not found in your profile: Docker."],
 "disclaimer": "Compatibility score based on your profile data; not a prediction of hiring outcome."}
```
UI tips: show the percentage with the confidence label; show `missing_*_skills` as "skills to add"; display the
disclaimer near the score. Inferred skills should be shown as "likely" rather than "confirmed".

## 8. Preferences — `GET` / `PUT /recommendations/preferences`
```json
{"preferred_job_titles": ["Python Developer"], "preferred_locations": ["Karachi"], "preferred_countries": ["pk"],
 "employment_types": ["FULLTIME"], "remote_preference": "any", "experience_level": "mid", "min_match_score": 40}
```
`remote_preference`: `any | remote_only | hybrid | onsite`; `experience_level`: `internship | entry | junior | mid | senior | lead | manager`.

## 9. Pagination
Standard `pagination {page, page_size, total, has_more}`. History: `GET /recommendations/history`.

## 10. Errors
`422 insufficient_profile` (details.missing lists what to add — prompt to upload a CV / add skills) ·
`422 cv_not_processed` · `404 job_not_found` · `429` (20/min).

## 11. Loading / empty states
Scoring takes ~0.1–0.7 s; show a progress indicator. Empty `items` → "No matching jobs yet — try searching for jobs"
(recommendations only consider jobs already discovered through searches and SharkHire listings).
