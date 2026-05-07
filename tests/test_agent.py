"""Tests for the Agent capabilities.

Mocks the outreach generator so no Anthropic API calls are made. The agent's
state-keeping logic, classification thresholds, follow-up surfacing, and the
mirror to feedback.db are all exercised against tmp SQLite DBs.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from role_radar.agent import (
    Agent,
    DEFAULT_BORDERLINE_THRESHOLD,
    DEFAULT_REVIEW_THRESHOLD,
)
from role_radar.models import (
    ATSType,
    CompanyType,
    CVSignals,
    Job,
    JobLocation,
    ScoreBreakdown,
    ScoredJob,
)
from role_radar.outreach import OutreachDraft
from role_radar.storage import ApplicationStatus, FeedbackAction, Storage


# ---- fixtures ------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "agent.db")
    yield s
    s.close()


@pytest.fixture
def feedback_db_path(tmp_path: Path) -> Path:
    return tmp_path / "feedback.db"


@pytest.fixture
def agent(storage: Storage, feedback_db_path: Path) -> Agent:
    return Agent(storage, feedback_db_path=feedback_db_path)


def _make_job(
    job_id: str,
    company: str,
    title: str,
    *,
    posted_days_ago: int = 1,
) -> Job:
    return Job(
        id=job_id,
        external_id=job_id.split("_", 1)[-1],
        company=company,
        company_slug=company.lower().replace(" ", "-"),
        company_type=CompanyType.AI_TOP_20,
        title=title,
        location=JobLocation(
            city="San Francisco", state="CA", raw_location="San Francisco, CA"
        ),
        apply_url=f"https://example.com/{job_id}",
        posted_date=datetime.utcnow() - timedelta(days=posted_days_ago),
        source_ats=ATSType.GREENHOUSE,
    )


def _make_scored(job: Job, score: float, rank: int = 1) -> ScoredJob:
    return ScoredJob(
        job=job,
        score=score,
        score_breakdown=ScoreBreakdown(),
        match_reasons=[f"score {score:.0f}"],
        rank=rank,
    )


# ---- fetch_new_matches ---------------------------------------------------


def test_fetch_new_matches_classifies_by_threshold(agent: Agent, storage: Storage):
    high = _make_job("a_high", "AcmeAI", "PM, Platform")
    mid = _make_job("a_mid", "AcmeAI", "PM, Growth")
    low = _make_job("a_low", "AcmeAI", "PM, Support")
    for j in (high, mid, low):
        storage.save_job(j)

    matches = agent.fetch_new_matches([
        _make_scored(high, 85.0, 1),
        _make_scored(mid, 60.0, 2),
        _make_scored(low, 30.0, 3),
    ])

    by_id = {m.scored_job.job.id: m for m in matches}
    assert by_id["a_high"].category == "must_review"
    assert by_id["a_mid"].category == "borderline"
    assert by_id["a_low"].category == "probably_skip"


def test_fetch_new_matches_initialises_new_applications(
    agent: Agent, storage: Storage
):
    job = _make_job("new_job", "AcmeAI", "PM")
    storage.save_job(job)

    agent.fetch_new_matches([_make_scored(job, 80.0)])

    row = storage.get_application(job.id)
    assert row is not None
    assert row["status"] == ApplicationStatus.NEW.value


def test_fetch_new_matches_skips_closed_applications(
    agent: Agent, storage: Storage
):
    job = _make_job("closed", "AcmeAI", "PM")
    storage.save_job(job)
    storage.upsert_application(job.id, ApplicationStatus.REJECTED)

    matches = agent.fetch_new_matches([_make_scored(job, 90.0)])

    # Closed applications should not appear in the queue at all.
    assert matches == []


def test_fetch_new_matches_marks_existing_active_application_as_not_new(
    agent: Agent, storage: Storage
):
    job = _make_job("existing", "AcmeAI", "PM")
    storage.save_job(job)
    storage.upsert_application(job.id, ApplicationStatus.SAVED)

    matches = agent.fetch_new_matches([_make_scored(job, 80.0)])

    assert len(matches) == 1
    assert matches[0].is_new is False
    assert matches[0].existing_status == ApplicationStatus.SAVED


# ---- classify_match ------------------------------------------------------


def test_classify_match_uses_thresholds(agent: Agent):
    job = _make_job("c", "AcmeAI", "PM")
    assert agent.classify_match(_make_scored(job, DEFAULT_REVIEW_THRESHOLD)) == "must_review"
    assert agent.classify_match(_make_scored(job, DEFAULT_BORDERLINE_THRESHOLD)) == "borderline"
    assert agent.classify_match(_make_scored(job, DEFAULT_BORDERLINE_THRESHOLD - 1)) == "probably_skip"


# ---- record_decision -----------------------------------------------------


def test_record_decision_writes_audit_event_and_status(
    agent: Agent, storage: Storage
):
    job = _make_job("rec", "AcmeAI", "PM, Platform")
    storage.save_job(job)

    agent.record_decision(
        job.id,
        FeedbackAction.SAVED,
        company=job.company,
        title=job.title,
    )

    events = storage.get_events(job.id)
    assert len(events) == 1
    assert events[0]["action"] == FeedbackAction.SAVED.value

    row = storage.get_application(job.id)
    assert row["status"] == ApplicationStatus.SAVED.value


def test_record_decision_applied_sets_timestamps(agent: Agent, storage: Storage):
    job = _make_job("apply", "AcmeAI", "PM")
    storage.save_job(job)

    agent.record_decision(
        job.id,
        FeedbackAction.APPLIED,
        company=job.company,
        title=job.title,
    )

    row = storage.get_application(job.id)
    assert row["applied_at"] is not None
    assert row["last_contact_at"] is not None


def test_record_decision_mirrors_to_feedback_db(
    agent: Agent, storage: Storage, feedback_db_path: Path
):
    job = _make_job("mirror", "AcmeAI", "Senior PM, AI")
    storage.save_job(job)

    agent.record_decision(
        job.id,
        FeedbackAction.SAVED,
        company=job.company,
        title=job.title,
    )

    assert feedback_db_path.exists()
    conn = sqlite3.connect(str(feedback_db_path))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT feedback, applied FROM job_feedback WHERE job_id = ?",
            (job.id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "like"
        assert row[1] == 0  # SAVED is not the same as APPLIED

        # Learned preferences should have been populated.
        cursor.execute(
            "SELECT preference_type, preference_key FROM learned_preferences"
        )
        learned = cursor.fetchall()
        assert ("company", "AcmeAI") in learned
        # title contains "Senior" so seniority:senior key should appear
        assert any(p == ("title_keyword", "seniority:senior") for p in learned)
    finally:
        conn.close()


def test_record_decision_skipped_with_company_reason_marks_company_dislike(
    agent: Agent, storage: Storage, feedback_db_path: Path
):
    job = _make_job("skip", "AcmeAI", "PM")
    storage.save_job(job)

    agent.record_decision(
        job.id,
        FeedbackAction.SKIPPED,
        reason="company",
        company=job.company,
        title=job.title,
    )

    conn = sqlite3.connect(str(feedback_db_path))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT preference_type, preference_key, weight_adjustment FROM learned_preferences"
        )
        learned = {(t, k): w for t, k, w in cursor.fetchall()}
        # company should be present and negative
        assert learned[("company", "AcmeAI")] == -1.0
    finally:
        conn.close()


def test_record_decision_skipped_with_role_reason_does_not_mark_company(
    agent: Agent, storage: Storage, feedback_db_path: Path
):
    """A 'role' dislike should not penalize the company — it's the role that
    didn't fit, not the employer."""
    job = _make_job("skip2", "AcmeAI", "PM")
    storage.save_job(job)

    agent.record_decision(
        job.id,
        FeedbackAction.SKIPPED,
        reason="role",
        company=job.company,
        title=job.title,
    )

    conn = sqlite3.connect(str(feedback_db_path))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT preference_type, preference_key FROM learned_preferences"
        )
        learned = cursor.fetchall()
        assert ("company", "AcmeAI") not in learned
    finally:
        conn.close()


# ---- surface_followups ---------------------------------------------------


def test_surface_followups_returns_stale_applications(
    agent: Agent, storage: Storage
):
    fresh_job = _make_job("fresh", "AcmeAI", "PM")
    stale_job = _make_job("stale", "OtherCo", "PM")
    storage.save_job(fresh_job)
    storage.save_job(stale_job)

    storage.upsert_application(
        fresh_job.id,
        ApplicationStatus.APPLIED,
        applied_at=datetime.utcnow() - timedelta(days=2),
        last_contact_at=datetime.utcnow() - timedelta(days=2),
    )
    storage.upsert_application(
        stale_job.id,
        ApplicationStatus.APPLIED,
        applied_at=datetime.utcnow() - timedelta(days=20),
        last_contact_at=datetime.utcnow() - timedelta(days=20),
    )

    followups = agent.surface_followups(stale_days=14)
    assert [f.job_id for f in followups] == [stale_job.id]
    assert followups[0].days_since_contact >= 14


def test_surface_followups_ignores_closed_statuses(
    agent: Agent, storage: Storage
):
    job = _make_job("closed_old", "AcmeAI", "PM")
    storage.save_job(job)
    storage.upsert_application(
        job.id,
        ApplicationStatus.REJECTED,
        last_contact_at=datetime.utcnow() - timedelta(days=30),
    )

    assert agent.surface_followups(stale_days=14) == []


# ---- draft_outreach (no real API) ----------------------------------------


def test_draft_outreach_persists_draft_and_advances_status(
    agent: Agent, storage: Storage
):
    job = _make_job("draft1", "AcmeAI", "PM, Platform")
    storage.save_job(job)
    cv = CVSignals(raw_text="cv text", skills=["llm"], domains=["ai/ml"])

    fake = OutreachDraft(
        subject="Re: PM, Platform",
        body="Body. " * 30,
        rationale="Specific to AcmeAI's recent shipping cadence.",
        self_rating=7,
    )
    fake_generator = MagicMock()
    fake_generator.generate.return_value = fake

    draft, draft_id = agent.draft_outreach(
        job=job,
        cv_signals=cv,
        candidate_name="Test Candidate",
        contact_name="Sam",
        generator=fake_generator,
    )

    assert draft is fake
    assert draft_id > 0
    assert fake_generator.generate.called

    drafts = storage.get_drafts(job.id)
    assert len(drafts) == 1
    assert drafts[0]["subject"] == fake.subject
    assert drafts[0]["self_rating"] == 7

    row = storage.get_application(job.id)
    assert row["status"] == ApplicationStatus.OUTREACH_DRAFTED.value
    assert row["contact_name"] == "Sam"

    events = storage.get_events(job.id)
    assert any(e["action"] == FeedbackAction.DRAFTED.value for e in events)


# ---- pipeline_summary ----------------------------------------------------


def test_pipeline_summary_splits_active_from_closed(
    agent: Agent, storage: Storage
):
    a = _make_job("a", "X", "PM")
    b = _make_job("b", "Y", "PM")
    c = _make_job("c", "Z", "PM")
    for j in (a, b, c):
        storage.save_job(j)
    storage.upsert_application(a.id, ApplicationStatus.APPLIED)
    storage.upsert_application(b.id, ApplicationStatus.SAVED)
    storage.upsert_application(c.id, ApplicationStatus.REJECTED)

    summary = agent.pipeline_summary()
    assert summary["active"] == 2
    assert summary["closed"] == 1
    assert summary["total"] == 3
    assert summary["by_status"][ApplicationStatus.APPLIED.value] == 1
