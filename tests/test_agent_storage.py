"""Tests for the agent-related storage methods.

Covers the new tables added in support of `role_radar.agent`: applications,
outreach_drafts, and feedback_events. The pre-existing storage methods are
covered elsewhere; these tests are scoped to what the agent layer adds.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from role_radar.models import ATSType, CompanyType, Job, JobLocation
from role_radar.storage import ApplicationStatus, FeedbackAction, Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    """Fresh Storage on a tmp DB. Closes after the test."""
    db_path = tmp_path / "agent.db"
    s = Storage(db_path)
    yield s
    s.close()


@pytest.fixture
def sample_job() -> Job:
    return Job(
        id="anthropic_pm-claude",
        external_id="pm-claude",
        company="Anthropic",
        company_slug="anthropic",
        company_type=CompanyType.AI_TOP_20,
        title="Product Manager, Claude",
        location=JobLocation(
            city="San Francisco",
            state="CA",
            remote=False,
            raw_location="San Francisco, CA",
        ),
        apply_url="https://example.com/apply",
        source_ats=ATSType.GREENHOUSE,
    )


def _save_job(storage: Storage, job: Job) -> None:
    """Persist the sample job so application FKs resolve."""
    storage.save_job(job)


# ---- applications --------------------------------------------------------


def test_upsert_application_creates_new_row(storage: Storage, sample_job: Job):
    _save_job(storage, sample_job)

    storage.upsert_application(sample_job.id, ApplicationStatus.SAVED)

    row = storage.get_application(sample_job.id)
    assert row is not None
    assert row["status"] == ApplicationStatus.SAVED.value
    assert row["created_at"] == row["updated_at"]


def test_upsert_application_updates_status_only_writes_supplied_fields(
    storage: Storage, sample_job: Job
):
    """Passing None for a field should leave it unchanged (not overwrite to NULL)."""
    _save_job(storage, sample_job)

    storage.upsert_application(
        sample_job.id,
        ApplicationStatus.SAVED,
        notes="initial note",
        contact_name="Jane Doe",
    )
    storage.upsert_application(
        sample_job.id,
        ApplicationStatus.OUTREACH_DRAFTED,
        # notes and contact_name omitted: should persist
    )

    row = storage.get_application(sample_job.id)
    assert row["status"] == ApplicationStatus.OUTREACH_DRAFTED.value
    assert row["notes"] == "initial note"
    assert row["contact_name"] == "Jane Doe"


def test_list_applications_filters_by_status(storage: Storage, sample_job: Job):
    _save_job(storage, sample_job)

    other = sample_job.model_copy(update={"id": "other_xyz"})
    storage.save_job(other)

    storage.upsert_application(sample_job.id, ApplicationStatus.APPLIED)
    storage.upsert_application(other.id, ApplicationStatus.SAVED)

    saved = storage.list_applications(status=ApplicationStatus.SAVED)
    applied = storage.list_applications(status=ApplicationStatus.APPLIED)
    assert len(saved) == 1 and saved[0]["job_id"] == other.id
    assert len(applied) == 1 and applied[0]["job_id"] == sample_job.id


def test_pipeline_counts(storage: Storage, sample_job: Job):
    _save_job(storage, sample_job)
    other = sample_job.model_copy(update={"id": "other_xyz"})
    storage.save_job(other)

    storage.upsert_application(sample_job.id, ApplicationStatus.APPLIED)
    storage.upsert_application(other.id, ApplicationStatus.APPLIED)

    counts = storage.get_pipeline_counts()
    assert counts == {ApplicationStatus.APPLIED.value: 2}


# ---- outreach drafts -----------------------------------------------------


def test_save_and_get_drafts_returns_newest_first(storage: Storage, sample_job: Job):
    _save_job(storage, sample_job)

    first_id = storage.save_draft(
        sample_job.id, subject="Sub 1", body="Body 1", self_rating=7
    )
    second_id = storage.save_draft(
        sample_job.id, subject="Sub 2", body="Body 2", self_rating=8
    )

    drafts = storage.get_drafts(sample_job.id)
    assert len(drafts) == 2
    assert drafts[0]["id"] == second_id  # newest first
    assert drafts[1]["id"] == first_id


def test_mark_draft_sent_sets_flag_and_timestamp(storage: Storage, sample_job: Job):
    _save_job(storage, sample_job)
    draft_id = storage.save_draft(sample_job.id, subject="x", body="y")

    storage.mark_draft_sent(draft_id)

    drafts = storage.get_drafts(sample_job.id)
    assert drafts[0]["sent"] == 1
    assert drafts[0]["sent_at"] is not None


# ---- feedback events -----------------------------------------------------


def test_record_event_appends_audit_row(storage: Storage, sample_job: Job):
    _save_job(storage, sample_job)

    storage.record_event(sample_job.id, FeedbackAction.SAVED, reason=None)
    storage.record_event(
        sample_job.id,
        FeedbackAction.SKIPPED,
        reason="seniority",
        metadata={"hint": "too senior"},
    )

    events = storage.get_events(sample_job.id)
    assert [e["action"] for e in events] == [
        FeedbackAction.SAVED.value,
        FeedbackAction.SKIPPED.value,
    ]
    assert events[1]["reason"] == "seniority"
    # metadata is stored as JSON string
    assert "too senior" in events[1]["metadata"]


def test_get_job_by_id_round_trip(storage: Storage, sample_job: Job):
    _save_job(storage, sample_job)
    got = storage.get_job_by_id(sample_job.id)
    assert got is not None
    assert got.id == sample_job.id
    assert got.company == sample_job.company
    assert got.title == sample_job.title


def test_get_job_by_id_returns_none_for_missing(storage: Storage):
    assert storage.get_job_by_id("nope") is None
