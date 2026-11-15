# Flutter ↔ Saved Jobs, Applications, Interviews

Bearer token required everywhere; users only ever see their own records (others' ids → 404).
Saving and applying are separate actions. Recording an application for an external (JSearch) job does **not**
submit anything to the employer.

## Saved jobs
| Method | Path | Notes |
|---|---|---|
| POST | `/saved-jobs/{job_id}` | body optional `{"notes": "..."}`; `409 already_saved` |
| GET | `/saved-jobs` | `q, company, location, remote, employment_type, sort=saved_at|posted_at|title|match, page, page_size` |
| GET | `/saved-jobs/{job_id}` | |
| GET | `/saved-jobs/check/{job_id}` | `{is_saved, saved_job_id}` |
| DELETE | `/saved-jobs/{job_id}` | |
```dart
await http.post(Uri.parse('$baseUrl/saved-jobs/$jobId'), headers: await authHeaders());
final saved = jsonDecode((await http.get(Uri.parse('$baseUrl/saved-jobs?sort=saved_at'), headers: await authHeaders())).body)['data'];
```
`match_percentage` is a snapshot from recommendations at save time (may be null).

## Applications
Statuses: `interested, saved, applied, application_viewed, shortlisted, interview_scheduled, interview_completed,
assessment, offer_received, rejected, withdrawn, hired`.
| Method | Path | Notes |
|---|---|---|
| POST | `/applications` | `job_id` (stored job) **or** `job_title` (+company, location, apply URL) |
| GET | `/applications` | filters: `status` (repeatable), `company, title, location, source, employment_type, applied_from, applied_to, deadline_before, follow_up_before, interview_from, interview_to`, `sort=updated_at|created_at|applied_at|deadline|company` |
| GET | `/applications/{id}` | includes `history[]`, `interviews[]`, `job` |
| PUT | `/applications/{id}` | notes, deadline, follow_up_date, reminder_text, … (not status) |
| PATCH | `/applications/{id}/status` | `{"status": "...", "notes": "..."}` — every change appended to history |
| GET | `/applications/{id}/history` | |
| DELETE | `/applications/{id}` | permanent (history & interviews too) |
| GET | `/applications/reminders?days=14` | deadlines, follow-ups, interviews (overdue ≤7 days flagged) |
| GET | `/applications/statistics` | counts, conversion rates with `definitions` |

```dart
// Create from a stored job
await http.post(Uri.parse('$baseUrl/applications'), headers: await authHeaders(),
    body: jsonEncode({'job_id': jobId, 'status': 'applied', 'discovered_via': 'search',
                      'deadline': '2026-12-01', 'notes': 'Referred by Ali'}));
// Change status
await http.patch(Uri.parse('$baseUrl/applications/$appId/status'), headers: await authHeaders(),
    body: jsonEncode({'status': 'shortlisted', 'notes': 'Recruiter called'}));
// History
final hist = jsonDecode((await http.get(Uri.parse('$baseUrl/applications/$appId/history'), headers: await authHeaders())).body)['data'];
```
Any status may be chosen (to correct mistakes); `400 status_unchanged` if identical. Duplicate application for the same
stored job → `409 duplicate_application`.

**SharkHire employer jobs** (`source: "sharkhire"`): creating the application *submits* it to the employer.
Requires role job_seeker and `selected_cv_id`; optional `cover_letter`. Response includes `employer_status`
(the employer's pipeline, read-only for you). To leave: set status `withdrawn` (final). Deleting requires withdrawing
first (`409 withdraw_first`).

## Interviews
| Method | Path |
|---|---|
| POST | `/applications/{id}/interviews` |
| GET | `/applications/{id}/interviews` |
| GET | `/interviews/upcoming?days=30` |
| PUT | `/interviews/{interview_id}` |
| DELETE | `/interviews/{interview_id}` |
```dart
await http.post(Uri.parse('$baseUrl/applications/$appId/interviews'), headers: await authHeaders(),
    body: jsonEncode({'interview_type': 'video', 'interview_round': 1,
                      'scheduled_at': '2026-12-03T15:00:00', 'timezone': 'Asia/Karachi',
                      'location_or_meeting_link': 'https://meet.example.com/abc'}));
```
Types: `technical, hr, phone, video, in_person, other`; outcomes `pending, passed, failed, cancelled, rescheduled`.
A naive `scheduled_at` is interpreted in `timezone`; responses give UTC `scheduled_at` and `local_time`.
By default an earlier-stage application moves to `interview_scheduled` (`update_application_status: false` to skip).

## Reminders & notifications
`GET /applications/reminders` returns data only — **no push notifications are sent by the backend**. Schedule local
notifications in the app (e.g. `flutter_local_notifications`) from this list.

## Statistics
```dart
final s = jsonDecode((await http.get(Uri.parse('$baseUrl/applications/statistics'), headers: await authHeaders())).body)['data'];
// s['total_applications'], s['by_status'], s['interview_conversion_rate'] (null when no applications), s['definitions']
```
