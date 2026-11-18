# Profile & CV API

All endpoints require `Authorization: Bearer <token>` and only ever act on the caller's own data
(another user's CV id returns 404).

## Profile
| Method | Path | Notes |
|---|---|---|
| GET | `/profile/me` | Profile + `profile_completion` (0–100) + `completion_breakdown` + short-lived `profile_photo_url` |
| PUT | `/profile/me` | Full replace of editable fields (omitted → cleared) |
| PATCH | `/profile/me` | Partial update |
| POST | `/profile/me/photo` | multipart `file` (JPEG/PNG/WebP ≤ 2 MB); re-encoded to strip EXIF/GPS |
| DELETE | `/profile/me/photo` | |

Editable fields: `full_name, phone, headline, location, country (ISO-2), city, professional_summary,
current_job_title, years_of_experience, education[], experience[], skills[], certifications[],
preferred_job_titles[], preferred_employment_types[] (FULLTIME|PARTTIME|CONTRACTOR|INTERN), preferred_locations[]`.
Unknown fields (including `role`, `email`, `user_id`) → `422`. Skills are canonicalised (`postgres` → `PostgreSQL`);
unknown skills are kept as typed.

**Completion formula** (sum = 100): full_name 10 · headline 10 · location (city/country/location) 10 ·
summary ≥30 chars 15 · ≥3 skills 15 · ≥1 education 10 · experience entry or years 15 · a successfully processed CV 15.

## CV
| Method | Path | Notes |
|---|---|---|
| POST | `/cv/upload` | multipart `file`; PDF or DOCX ≤ 5 MB; max 10 CVs/user; first CV becomes primary |
| GET | `/cv` | list (primary first) |
| GET | `/cv/{cv_id}` | metadata + `processing_status` |
| GET | `/cv/{cv_id}/download` | `{url, expires_in, filename}` short-lived signed URL |
| GET | `/cv/{cv_id}/extracted-data` | skills, experience, education, certifications, years, confidences |
| PATCH | `/cv/{cv_id}/extracted-data` | user corrections → `is_user_confirmed: true` |
| POST | `/cv/{cv_id}/apply-to-profile` | copy extracted data into the profile (`fields`, `overwrite`) |
| POST | `/cv/{cv_id}/process` | re-run extraction (resets confirmation) |
| POST | `/cv/{cv_id}/primary` | make primary (exactly one primary per user, DB-enforced) |
| DELETE | `/cv/{cv_id}` | deletes file + extracted data atomically; newest remaining CV becomes primary |

### Validation (server never trusts filename or Content-Type alone)
1. extension `.pdf`/`.docx`; 2. declared MIME must match (or be `application/octet-stream`);
3. size streamed with a hard cap; 4. magic bytes (`%PDF-` / ZIP with `word/document.xml`);
5. structural open with PyMuPDF / python-docx; 6. DOCX: zip-bomb limits, macros (`vbaProject.bin`) rejected;
7. duplicate content (SHA-256) per user → `409 duplicate_cv` with the existing `cv_id`.
Storage paths are server-generated (`cvs/<user-uuid>/<random>.pdf`); client filenames are only display names.

### `processing_status`
| Value | Meaning |
|---|---|
| `completed` | text extracted and parsed |
| `no_text` | readable document but no text |
| `ocr_required` | scanned/image-only PDF — **OCR is not implemented** |
| `failed` | encrypted/password-protected, too many pages (>30) or unreadable; see `extraction_error` |
| `processing` / `pending` | transient |

### Extraction (rule-based, no paid AI)
Name (heuristic), email, phone, skills (taxonomy of ~150 skills with aliases; ambiguous words like "Go", "R", "ML"
only accepted inside a Skills section), work experience (title/company/date ranges), total years (merged ranges,
internships excluded; falls back to "X years of experience" statements), education (degree/institution/years),
certifications. Nothing is invented: unknown → `null`/`[]`. `extraction_metadata.confidence` gives per-field confidence.

### Errors
`413 file_too_large`, `415 unsupported_file_type | mime_mismatch | invalid_file_signature | macro_not_allowed`,
`422 empty_file | corrupted_file`, `409 duplicate_cv | cv_limit_reached`,
`503 storage_not_configured | storage_bucket_missing | storage_auth_failed | storage_unavailable`
(all 503 storage codes are server-side configuration/outage problems; the app should show a generic
"uploads unavailable, try again later" message — check `GET /api/v1/health/storage`).

### Upload response (additive fields, 2026-11-19)
`POST /cv/upload` still returns the CV metadata and `skills_found`, plus:
* `extracted_data` — `{skills, experience, education, qualifications, job_titles, years_of_experience,
  is_user_confirmed}` for the review screen; `null` when `processing_status` is not `completed`.
  `qualifications` = certifications/licences found in the CV. Nothing is applied to the profile until the
  user confirms (`PATCH /cv/{id}/extracted-data`) / applies it (`POST /cv/{id}/apply-to-profile`).
* `replaced_cv_id` — set when `?replace=true` was used and the previous primary CV was deleted.

`?replace=true` (query parameter): the new CV becomes primary and the previous primary CV (file + data) is
deleted **after** the new one is stored and recorded. If that deletion fails the new CV is still primary and the
old one stays listed (`replaced_cv_id: null`). Allowed even when the user is at `CV_MAX_FILES_PER_USER`.

### Current-CV convenience endpoints
| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/cv/me` | `{cv, extracted_data}` for the primary CV, or both `null` when none |
| GET | `/api/v1/cv/status` | `{has_cv, cv_id, processing_status, extraction_error, is_reviewed, total_cvs, uploaded_at, processed_at}` |
| DELETE | `/api/v1/cv/me` | deletes the primary CV (404 `cv_not_found` when none); newest remaining CV becomes primary |
