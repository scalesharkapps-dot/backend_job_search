# Recommendation Scoring — How SharkHire computes a match

The match percentage is a **compatibility score** derived from your profile/CV and the job text.
It is **not** a probability of being hired and has not been statistically validated.

## Inputs
**Candidate** (stored data only, nothing invented): profile skills (user-confirmed) → confirmed CV data →
extracted CV data; skills implied by others (FastAPI ⇒ Python) are marked *implied*. Titles from current title,
headline, experience, CV job titles and preferred titles. Years of experience: profile → CV (internships excluded).
Location & preferences from profile and `/recommendations/preferences`. The primary CV is used unless `cv_id` is given.

**Job**: explicit requirements first (provider `job_required_skills`, employer required/preferred skills, stated
experience). Otherwise skills are *inferred* from the description/highlights: a sentence with a preference cue
("nice to have", "a plus", "preferred", "bonus") yields preferred skills, other mentions are required.
Experience: explicit months/years → "X+ years" in text → seniority word in the title (intern 0, junior 0, mid 2,
senior 5, lead 7). Derived requirements are cached per job (`jobs.derived_requirements`).

## Components (each 0–100 or *not applicable*)
| Component | Default weight | Rule |
|---|---|---|
| Required skills | 40 | (matched + 0.75·implied) / required |
| Preferred skills | 15 | same formula |
| Experience | 20 | meets → 100; overqualified → 75; ≥80% of min → 85; ≥60% → 65; ≥40% → 40; else 15 |
| Title relevance | 10 | token overlap of normalised titles; same domain family (e.g. Python ↔ Backend) → 50; generic words ignored |
| Location | 10 | remote job → 100 (50 if on-site preferred; 85 if remote in another country); same city/preferred → 100; same country → 70; different → 15; remote-only pref vs on-site → 10 |
| Employment type | 5 | overlap → 100, mismatch → 0 |

Weights are configurable (`REC_WEIGHT_*`).

## Formula
```
match = Σ(weight_i × score_i) / Σ(weight_i)      over applicable components only
```
Missing data never becomes 0 or 100: the component is dropped and weights renormalised (`weights_used`).

**Relevance gate** — context (experience, location, employment) cannot make an unrelated job look good:
`core` = weighted mean of the applicable skill/title components. If no skill/title component applies the score is
capped at 40; if `core < 40` the score is capped at `core + 15`. Reported as `relevance_gate`.

**Confidence** = (applicable weight / total weight) × quality, quality = 1.0 explicit job skills, 0.85 inferred, 0.7 none.
Labels: high ≥ 0.70, medium ≥ 0.45, low otherwise.

## Ranking
Sort by `match + freshness bonus` (≤3 days +3, ≤7 +2, ≤30 +1; the bonus is **not** added to the shown match),
then confidence, recency, id — fully deterministic. Jobs below `REC_MIN_SCORE` (20, or the user's `min_match_score`)
are hidden; `experience_level` preference hides jobs more than two levels away; `remote_only` preference filters to
remote jobs.

## Candidate jobs
External jobs seen in searches within `REC_JOB_MAX_AGE_DAYS` (45) and not reported gone, plus published SharkHire
listings — up to `REC_MAX_CANDIDATE_JOBS` (500), newest first. Recommendations do **not** call JSearch; users widen
the pool by searching.

## Explanations
`reasons` / `concerns` are generated only from computed component results (matched skills, experience verdict, title
relevance, location, employment, gate, low confidence).

## Performance (measured locally, 2026-11-15)
500 candidate jobs on PostgreSQL 16 (Docker), 1 Uvicorn worker on a Windows dev machine:
pure scoring ≈ 47 ms; full `GET /recommendations/for-me` ≈ 140 ms median uncontended (≈ 670 ms on the very first
call while job requirements are derived and cached). The endpoint is CPU-bound: 20 concurrent calls on one worker
queue (p50 ≈ 3.3 s, 0 errors). Scale with `WEB_CONCURRENCY`; per-user limit 20/min.

## Limitations
Rule-based skill matching (no semantic similarity); title families are hand-curated; inferred requirements can be wrong
when descriptions are vague; non-English descriptions are poorly supported; location matching is string-based.
