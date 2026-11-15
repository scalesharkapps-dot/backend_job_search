"""Phase 5 — recommendation engine tests (unit + API on the test database)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.models.base import utcnow
from app.models.job import Job, JobSource
from app.services.recommendations.matchers import (
    CandidateProfile,
    JobFacts,
    employment_component,
    experience_component,
    location_component,
    skill_component,
    title_component,
    title_similarity,
)
from app.services.recommendations.requirements import derive_requirements, infer_skills_from_text, years_from_text
from app.services.recommendations.scoring import score_components
from tests.documents import PDF_MIME, make_pdf

WEIGHTS = {"required_skills": 40, "preferred_skills": 15, "experience": 20, "title": 10, "location": 10, "employment": 5}


def cand(**kw) -> CandidateProfile:
    base = {"skills": {"Python": "confirmed", "FastAPI": "confirmed", "SQL": "extracted", "PostgreSQL": "confirmed"},
                "titles": ["Python Developer"], "years_of_experience": 4.0, "city": "Karachi", "country": "pk",
                "employment_types": ["FULLTIME"]}
    base.update(kw)
    return CandidateProfile(**base)


def facts(**kw) -> JobFacts:
    base = {"title": "Python Developer", "city": "Karachi", "country": "pk", "location": "Karachi, PK", "is_remote": False,
                "employment_types": ["FULLTIME"]}
    base.update(kw)
    return JobFacts(**base)


# ---------------- Skills ----------------
@pytest.mark.unit
def test_skill_exact_case_alias_and_missing():
    c = cand()
    r = skill_component(c, ["Python", "PostgreSQL", "Docker"], "required")
    assert r.details["matched"] == ["Python", "PostgreSQL"] and r.details["missing"] == ["Docker"]
    assert r.score == pytest.approx(66.7, abs=0.1)


@pytest.mark.unit
def test_implied_skill_partial_credit():
    c = cand(skills={"FastAPI": "confirmed", "Python": "implied"})
    r = skill_component(c, ["Python"], "required")
    assert r.details["matched_implied"] == ["Python"] and r.score == 75.0


@pytest.mark.unit
def test_unrelated_skills_do_not_match():
    c = cand(skills={"Java": "confirmed", "JavaScript": "confirmed"})
    r = skill_component(c, ["Python", "Go"], "required")
    assert r.score == 0.0


@pytest.mark.unit
def test_skill_component_not_applicable():
    assert skill_component(cand(), [], "required").score is None
    assert skill_component(cand(skills={}), ["Python"], "required").score is None


@pytest.mark.unit
def test_required_vs_preferred_inference():
    text = "You must know Python and FastAPI. Experience with Docker is a plus. AWS preferred."
    required, preferred = infer_skills_from_text(text)
    assert required == ["Python", "FastAPI"]
    assert set(preferred) == {"Docker", "AWS"}
    required, preferred = infer_skills_from_text("Python is a plus. Python is required.")
    assert required == ["Python"] and preferred == []


# ---------------- Experience ----------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "title,text,months,expected_min,source",
    [
        ("Software Engineering Intern", "", None, 0, "title_level"),
        ("Entry Level Analyst", "", None, 0, "title_level"),
        ("Junior Developer", "", None, 0, "title_level"),
        ("Developer", "Minimum 3 years of professional experience", None, 3, "stated_in_text"),
        ("Senior Engineer", "", None, 5, "title_level"),
        ("Engineer", "2-4 years experience", None, 2, "stated_in_text"),
        ("Engineer", "", 36, 3, "explicit"),
        ("Engineer", "Great team", None, None, "none"),
    ],
)
def test_experience_requirement_derivation(title, text, months, expected_min, source):
    req = derive_requirements(title=title, description=text, highlights={}, required_experience_months=months)
    assert req.min_years == expected_min and req.experience_source == source


@pytest.mark.unit
def test_years_from_text_ignores_noise():
    assert years_from_text("Company founded 25 years ago. 3+ years of Python experience required") == (3.0, None)
    assert years_from_text("5-7 years of relevant experience")[1] == 7.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "years,min_years,max_years,level,label,score",
    [(5, 3, None, None, "meets", 100), (2.5, 3, None, None, "slightly_below", 85), (1, 3, None, None, "below", 15),
     (12, 0, None, "junior", "overqualified", 75), (0, 0, None, "internship", "meets", 100)],
)
def test_experience_component(years, min_years, max_years, level, label, score):
    req = derive_requirements(title="x", description="", highlights={}, min_years=min_years, max_years=max_years, level=level)
    r = experience_component(cand(years_of_experience=years), req)
    assert (r.label, r.score) == (label, score)


@pytest.mark.unit
def test_experience_missing_data_not_zero():
    req = derive_requirements(title="Engineer", description="", highlights={})
    assert experience_component(cand(), req).score is None  # job silent
    req = derive_requirements(title="x", description="", highlights={}, min_years=3)
    assert experience_component(cand(years_of_experience=None), req).score is None  # candidate unknown


# ---------------- Title / location / employment ----------------
@pytest.mark.unit
def test_title_relevance():
    assert title_similarity("Python Developer", "Senior Python Developer") == 1.0
    assert title_similarity("Python Developer", "Backend Engineer") == 0.5
    assert title_similarity("Machine Learning Engineer", "AI Engineer") == 0.5
    assert title_similarity("Software Engineer", "Mechanical Engineer") == 0.0  # share only a generic word
    assert title_similarity("Data Analyst", "Python Developer") == 0.0
    assert title_component(cand(titles=[]), "Anything").score is None
    assert title_component(cand(preferred_titles=["Data Scientist"], titles=[]), "Data Scientist").label == "highly_relevant"


@pytest.mark.unit
def test_location_rules():
    c = cand()
    assert location_component(c, facts()).label == "compatible"
    assert location_component(c, facts(city="Lahore", location="Lahore, PK")).label == "same_country"
    assert location_component(c, facts(city="London", country="gb", location="London, GB")).label == "different_location"
    assert location_component(c, facts(is_remote=True)).score == 100
    assert location_component(cand(remote_preference="remote_only"), facts()).score == 10
    assert location_component(cand(remote_preference="onsite"), facts(is_remote=True)).score == 50
    assert location_component(c, facts(is_remote=True, country="us")).label == "remote_other_country"
    assert location_component(cand(city=None, country=None), facts()).score is None


@pytest.mark.unit
def test_employment_rules():
    assert employment_component(cand(), facts()).score == 100
    assert employment_component(cand(), facts(employment_types=["CONTRACTOR"])).score == 0
    assert employment_component(cand(employment_types=[]), facts()).score is None
    assert employment_component(cand(), facts(employment_types=[])).score is None


# ---------------- Scoring ----------------
@pytest.mark.unit
def test_full_match_scores_100():
    req = derive_requirements(title="Python Developer", description="", highlights={}, explicit_required=["Python", "FastAPI"],
                              explicit_preferred=["PostgreSQL"], min_years=3)
    r = score_components(cand(), req, facts(), WEIGHTS)
    assert r.match_percentage == 100 and r.confidence_label == "high"
    assert r.matched_skills == ["Python", "FastAPI", "PostgreSQL"]


@pytest.mark.unit
def test_weighted_formula_and_renormalisation():
    # required 50, title 100, location 100, employment 100; preferred & experience not applicable.
    req = derive_requirements(title="Python Developer", description="", highlights={}, explicit_required=["Python", "Docker"])
    r = score_components(cand(), req, facts(), WEIGHTS)
    expected = (50 * 40 + 100 * 10 + 100 * 10 + 100 * 5) / (40 + 10 + 10 + 5)
    assert r.match_percentage == round(expected)
    assert r.score_confidence == round((65 / 100) * 1.0, 2)
    assert set(r.weights_used) == {"required_skills", "title", "location", "employment"}


@pytest.mark.unit
def test_poor_match_and_bounds():
    req = derive_requirements(title="Registered Nurse", description="", highlights={}, explicit_required=["Customer Service", "Communication"], min_years=8)
    r = score_components(cand(years_of_experience=1, city="Lahore"), req, facts(title="Registered Nurse", city="Dubai", country="ae", location="Dubai"), WEIGHTS)
    assert 0 <= r.match_percentage < 25
    assert r.missing_required_skills == ["Customer Service", "Communication"]
    assert r.concerns


@pytest.mark.unit
def test_relevance_gate_blocks_context_only_matches():
    # Unrelated job: no comparable skills, unrelated title, but experience/employment fit.
    req = derive_requirements(title="Registered Nurse", description="Patient care. 2 years experience.", highlights={})
    r = score_components(cand(), req, facts(title="Registered Nurse"), WEIGHTS)
    assert r.relevance_gate == "low_relevance" and r.match_percentage <= 15
    # No relevance component applicable at all (no titles, job lists no skills).
    r = score_components(cand(titles=[]), req, facts(title="Registered Nurse"), WEIGHTS)
    assert r.relevance_gate == "no_relevance_data" and r.match_percentage <= 40


@pytest.mark.unit
def test_inferred_skills_lower_confidence():
    explicit = derive_requirements(title="Dev", description="", highlights={}, explicit_required=["Python"])
    inferred = derive_requirements(title="Dev", description="We need Python.", highlights={})
    a = score_components(cand(), explicit, facts(), WEIGHTS)
    b = score_components(cand(), inferred, facts(), WEIGHTS)
    assert b.score_confidence < a.score_confidence and b.skills_source == "inferred"
    assert "inferred from the job description" in b.reasons[0]


@pytest.mark.unit
def test_deterministic():
    req = derive_requirements(title="Backend Engineer", description="Python, Docker and AWS. Kubernetes is a plus. 3+ years experience.", highlights={})
    results = {score_components(cand(), req, facts(title="Backend Engineer"), WEIGHTS).match_percentage for _ in range(5)}
    assert len(results) == 1


@pytest.mark.unit
def test_nothing_known_gives_zero_with_low_confidence():
    req = derive_requirements(title="Role", description="", highlights={})
    r = score_components(CandidateProfile(), req, facts(employment_types=[], city=None, country=None, location=None), WEIGHTS)
    assert r.match_percentage == 0 and r.score_confidence == 0 and r.confidence_label == "low"


# ---------------- API ----------------
async def add_job(db, **kw) -> Job:
    values = {"source": JobSource.JSEARCH, "external_id": uuid.uuid4().hex, "title": "Python Developer", "company_name": "Acme",
                  "description": "Python and FastAPI required. Docker is a plus. 3+ years of experience.", "city": "Karachi",
                  "country": "pk", "location": "Karachi, PK", "is_remote": False, "employment_type": "FULLTIME",
                  "employment_types": ["FULLTIME"], "posted_at": utcnow() - timedelta(days=1), "last_seen_at": utcnow(),
                  "highlights": {}, "required_skills": [], "apply_options": [], "provider_metadata": {}}
    values.update(kw)
    job = Job(**values)
    db.add(job)
    await db.commit()
    return job


async def setup_profile(client, headers, **overrides):
    body = {"full_name": "Ayesha", "city": "Karachi", "country": "PK", "skills": ["Python", "FastAPI", "PostgreSQL"],
            "current_job_title": "Python Developer", "years_of_experience": 4, "preferred_employment_types": ["FULLTIME"]}
    body.update(overrides)
    r = await client.put("/api/v1/profile/me", headers=headers, json=body)
    assert r.status_code == 200, r.text


async def test_for_me_ranks_and_explains(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    good = await add_job(db, title="Python Developer")
    await add_job(db, title="Backend Engineer", description="Java and Spring Boot required. 5+ years experience.")
    await add_job(db, title="Registered Nurse", description="Patient care, 2 years experience.", city="Dubai", country="ae", location="Dubai")
    r = await client.get("/api/v1/recommendations/for-me", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    items = data["items"]
    assert items[0]["job"]["id"] == str(good.id)
    match = items[0]["match"]
    assert match["match_percentage"] >= 80
    assert set(match["matched_skills"]) >= {"Python", "FastAPI"}
    assert match["missing_preferred_skills"] == ["Docker"]
    assert match["reasons"] and "not a prediction" in match["disclaimer"]
    assert [i["match"]["match_percentage"] for i in items] == sorted((i["match"]["match_percentage"] for i in items), reverse=True)
    assert all(i["job"]["job_title"] != "Registered Nurse" for i in items)  # below REC_MIN_SCORE
    assert data["candidate"]["data_sources"] == ["profile"]
    assert data["algorithm_version"]


async def test_insufficient_profile(client, db, user_factory):
    _, headers = await user_factory()
    await add_job(db)
    r = await client.get("/api/v1/recommendations/for-me", headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "insufficient_profile"
    assert len(r.json()["error"]["details"]["missing"]) == 2


async def test_empty_job_pool(client, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    r = await client.get("/api/v1/recommendations/for-me", headers=headers)
    assert r.status_code == 200 and r.json()["data"]["items"] == []
    assert "No matching jobs" in r.json()["message"]


async def test_stale_and_unavailable_jobs_excluded(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    await add_job(db, last_seen_at=utcnow() - timedelta(days=90))
    await add_job(db, unavailable_since=utcnow())
    r = await client.get("/api/v1/recommendations/for-me", headers=headers)
    assert r.json()["data"]["items"] == []


async def test_pagination_and_min_score(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    for i in range(5):
        await add_job(db, title=f"Python Developer {i}", posted_at=utcnow() - timedelta(days=i))
    page1 = (await client.get("/api/v1/recommendations/for-me?page=1&page_size=2", headers=headers)).json()["data"]
    page3 = (await client.get("/api/v1/recommendations/for-me?page=3&page_size=2", headers=headers)).json()["data"]
    assert len(page1["items"]) == 2 and page1["pagination"]["has_more"] is True and page1["pagination"]["total"] == 5
    assert len(page3["items"]) == 1 and page3["pagination"]["has_more"] is False
    none = (await client.get("/api/v1/recommendations/for-me?min_score=100&page_size=50", headers=headers)).json()["data"]
    assert all(i["match"]["match_percentage"] == 100 for i in none["items"])


async def test_preferences_affect_results(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    await add_job(db, title="Python Developer", is_remote=False)
    await add_job(db, title="Remote Python Developer", is_remote=True)
    r = await client.put("/api/v1/recommendations/preferences", headers=headers,
                         json={"remote_preference": "remote_only", "preferred_countries": ["PK"], "employment_types": ["FULLTIME"]})
    assert r.status_code == 200 and r.json()["data"]["preferred_countries"] == ["pk"]
    items = (await client.get("/api/v1/recommendations/for-me", headers=headers)).json()["data"]["items"]
    assert [i["job"]["job_is_remote"] for i in items] == [True]
    got = (await client.get("/api/v1/recommendations/preferences", headers=headers)).json()["data"]
    assert got["remote_preference"] == "remote_only"


async def test_preferences_validation_and_no_role_field(client, user_factory):
    _, headers = await user_factory()
    for body in [{"role": "admin"}, {"preferred_countries": ["Pakistan"]}, {"remote_preference": "sometimes"}, {"min_match_score": 101}]:
        assert (await client.put("/api/v1/recommendations/preferences", headers=headers, json=body)).status_code == 422


async def test_preferred_title_used(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers, current_job_title=None, skills=["SQL", "Excel", "Power BI"])
    await client.put("/api/v1/recommendations/preferences", headers=headers, json={"preferred_job_titles": ["Data Analyst"]})
    job = await add_job(db, title="Data Analyst", description="SQL and Power BI required.")
    data = (await client.get(f"/api/v1/recommendations/jobs/{job.id}/match", headers=headers)).json()["data"]
    assert data["match"]["title_match"] == "highly_relevant"


async def test_match_specific_job_and_history(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    job = await add_job(db)
    r = await client.get(f"/api/v1/recommendations/jobs/{job.id}/match", headers=headers)
    assert r.status_code == 200
    match = r.json()["data"]["match"]
    assert set(match["score_breakdown"]) == {"required_skills", "preferred_skills", "experience", "title", "location", "employment"}
    assert match["experience_match"] == "meets"
    hist = (await client.get("/api/v1/recommendations/history", headers=headers)).json()["data"]
    assert hist["pagination"]["total"] == 1 and hist["items"][0]["match_percentage"] == match["match_percentage"]
    # recomputation updates, never duplicates
    await client.get(f"/api/v1/recommendations/jobs/{job.id}/match", headers=headers)
    assert (await client.get("/api/v1/recommendations/history", headers=headers)).json()["data"]["pagination"]["total"] == 1


async def test_match_invalid_job(client, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    assert (await client.get(f"/api/v1/recommendations/jobs/{uuid.uuid4()}/match", headers=headers)).status_code == 404


async def test_cv_based_recommendations_and_multiple_cvs(client, db, user_factory):
    _, headers = await user_factory()
    cv1 = (await client.post("/api/v1/cv/upload", headers=headers, files={"file": ("a.pdf", make_pdf(), PDF_MIME)})).json()["data"]
    data_cv = "Bilal Ahmed\nbilal@example.com\nEXPERIENCE\nData Analyst at DataCo\nJan 2021 - Present\nSKILLS\nSQL, Excel, Power BI, Tableau"
    cv2 = (await client.post("/api/v1/cv/upload", headers=headers, files={"file": ("b.pdf", make_pdf(data_cv), PDF_MIME)})).json()["data"]
    py_job = await add_job(db, title="Python Developer")
    data_job = await add_job(db, title="Data Analyst", description="SQL, Excel and Power BI required. 2+ years experience.")

    default = (await client.get("/api/v1/recommendations/for-me", headers=headers)).json()["data"]
    assert default["candidate"]["cv_id"] == cv1["id"]  # primary CV
    assert default["items"][0]["job"]["id"] == str(py_job.id)
    assert "cv_extracted" in default["candidate"]["data_sources"]

    analysed = (await client.post("/api/v1/recommendations/analyze", headers=headers, json={"cv_id": cv2["id"]})).json()["data"]
    assert analysed["candidate"]["cv_id"] == cv2["id"]
    assert analysed["items"][0]["job"]["id"] == str(data_job.id)


async def test_cross_user_cv_rejected(client, db, user_factory):
    _, owner = await user_factory()
    _, other = await user_factory()
    cv = (await client.post("/api/v1/cv/upload", headers=owner, files={"file": ("a.pdf", make_pdf(), PDF_MIME)})).json()["data"]
    await add_job(db)
    r = await client.post("/api/v1/recommendations/analyze", headers=other, json={"cv_id": cv["id"]})
    assert r.status_code == 404
    r = await client.get(f"/api/v1/recommendations/for-me?cv_id={cv['id']}", headers=other)
    assert r.status_code == 404


async def test_unprocessed_cv_rejected(client, user_factory):
    from tests.documents import make_scanned_pdf

    _, headers = await user_factory()
    cv = (await client.post("/api/v1/cv/upload", headers=headers, files={"file": ("s.pdf", make_scanned_pdf(), PDF_MIME)})).json()["data"]
    r = await client.post("/api/v1/recommendations/analyze", headers=headers, json={"cv_id": cv["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "cv_not_processed"


async def test_recommendations_require_auth(client):
    assert (await client.get("/api/v1/recommendations/for-me")).status_code == 401


async def test_history_isolation(client, db, user_factory):
    _, a = await user_factory()
    _, b = await user_factory()
    await setup_profile(client, a)
    job = await add_job(db)
    await client.get(f"/api/v1/recommendations/jobs/{job.id}/match", headers=a)
    assert (await client.get("/api/v1/recommendations/history", headers=b)).json()["data"]["items"] == []


async def test_derived_requirements_cached(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    job = await add_job(db)
    await client.get("/api/v1/recommendations/for-me", headers=headers)
    await db.refresh(job)
    assert job.derived_requirements["required_skills"] == ["Python", "FastAPI"]


async def test_configurable_weights(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    job = await add_job(db, description="Java required.")
    before = (await client.get(f"/api/v1/recommendations/jobs/{job.id}/match", headers=headers)).json()["data"]["match"]["match_percentage"]
    get_settings().REC_WEIGHT_REQUIRED_SKILLS = 0
    after = (await client.get(f"/api/v1/recommendations/jobs/{job.id}/match", headers=headers)).json()["data"]["match"]["match_percentage"]
    assert after > before


async def test_concurrent_recommendations_same_user(client, db, user_factory):
    import asyncio

    _, headers = await user_factory()
    await setup_profile(client, headers)
    for i in range(5):
        await add_job(db, title=f"Python Developer {i}")
    results = await asyncio.gather(*(client.get("/api/v1/recommendations/for-me", headers=headers) for _ in range(6)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results if r.status_code != 200]
    hist = (await client.get("/api/v1/recommendations/history?page_size=50", headers=headers)).json()["data"]
    assert hist["pagination"]["total"] == 5  # upsert never duplicates
