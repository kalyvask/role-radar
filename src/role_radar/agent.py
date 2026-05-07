"""Stateful agent layer over Role Radar's batch pipeline.

The pipeline (scrape -> filter -> score -> rank) runs unchanged. The agent
sits on top, classifying scored jobs, surfacing follow-ups, drafting outreach,
and persisting decisions across runs.

Design: each capability is a discrete method that does one thing. The CLI
calls them in sequence with prompts; a future autonomous loop (the L scope)
calls the same methods without prompts. No procedural god-function in here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from role_radar.models import CVSignals, Job, ScoredJob
from role_radar.outreach import OutreachDraft, OutreachGenerationError, OutreachGenerator
from role_radar.storage import ApplicationStatus, FeedbackAction, Storage
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


# Score threshold above which a job is auto-surfaced as "must review."
# Below this, the agent classifies as borderline / probably_skip.
DEFAULT_REVIEW_THRESHOLD = 70.0
DEFAULT_BORDERLINE_THRESHOLD = 55.0

# How long since last_contact_at before a follow-up is suggested.
DEFAULT_FOLLOWUP_STALE_DAYS = 14


# Status that means "still in the agent's active queue, not closed out."
ACTIVE_STATUSES = {
    ApplicationStatus.NEW,
    ApplicationStatus.SAVED,
    ApplicationStatus.OUTREACH_DRAFTED,
    ApplicationStatus.OUTREACH_SENT,
    ApplicationStatus.APPLIED,
    ApplicationStatus.RESPONDED,
    ApplicationStatus.INTERVIEWING,
}

CLOSED_STATUSES = {
    ApplicationStatus.OFFER,
    ApplicationStatus.REJECTED,
    ApplicationStatus.WITHDRAWN,
    ApplicationStatus.SKIPPED,
}


@dataclass
class Match:
    """A scored job, classified by the agent into a review category."""

    scored_job: ScoredJob
    category: str  # "must_review" / "borderline" / "probably_skip"
    is_new: bool   # True if not seen in any prior agent run
    existing_status: Optional[ApplicationStatus] = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class FollowupCandidate:
    """An open application that may need a follow-up."""

    job_id: str
    company: str
    title: str
    status: ApplicationStatus
    days_since_contact: int
    last_action: str
    job: Optional[Job] = None


class Agent:
    """The stateful agent. Orchestrates capabilities, owns the storage handle.

    The agent does not start I/O on construction. Capabilities are independent
    and can be called in any order. Each method commits to storage before
    returning, so a crash mid-loop loses no state.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
        borderline_threshold: float = DEFAULT_BORDERLINE_THRESHOLD,
        followup_stale_days: int = DEFAULT_FOLLOWUP_STALE_DAYS,
        feedback_db_path: Optional[Path] = None,
    ):
        self.storage = storage
        self.review_threshold = review_threshold
        self.borderline_threshold = borderline_threshold
        self.followup_stale_days = followup_stale_days
        # Path to the existing web/app.py feedback DB so we can mirror decisions
        # to it and let the existing scoring path pick them up.
        self.feedback_db_path = feedback_db_path or (
            Path.home() / ".role_radar" / "feedback.db"
        )

    # ---- Capability 1: surface new matches from a fresh scored list -------

    def fetch_new_matches(self, scored_jobs: list[ScoredJob]) -> list[Match]:
        """Classify a fresh scored-job list against the pipeline state.

        Returns matches in three buckets via the `.category` field:
        - "must_review": score >= review_threshold AND not already closed out
        - "borderline": borderline_threshold <= score < review_threshold AND active
        - "probably_skip": below borderline OR already closed (filtered out
                           unless the user asked for full visibility)

        Skipped/rejected/withdrawn jobs are dropped (the user already decided).
        """
        matches: list[Match] = []
        for sj in scored_jobs:
            existing = self.storage.get_application(sj.job.id)
            existing_status = (
                ApplicationStatus(existing["status"]) if existing else None
            )

            # Skip jobs the user already closed out.
            if existing_status in CLOSED_STATUSES:
                continue

            is_new = existing is None
            score = sj.score
            reasons: list[str] = []

            if score >= self.review_threshold:
                category = "must_review"
                reasons.append(f"Score {score:.0f} above review threshold")
            elif score >= self.borderline_threshold:
                category = "borderline"
                reasons.append(f"Score {score:.0f} borderline")
            else:
                category = "probably_skip"
                reasons.append(f"Score {score:.0f} below threshold")

            if not is_new:
                reasons.append(f"Already in pipeline as {existing_status.value}")

            matches.append(
                Match(
                    scored_job=sj,
                    category=category,
                    is_new=is_new,
                    existing_status=existing_status,
                    reasons=reasons,
                )
            )

        # Initialize NEW applications so the next run can tell them apart.
        for m in matches:
            if m.is_new and m.category in ("must_review", "borderline"):
                self.storage.upsert_application(
                    m.scored_job.job.id,
                    ApplicationStatus.NEW,
                )

        logger.info(
            "agent_fetch_new_matches",
            total=len(matches),
            must_review=sum(1 for m in matches if m.category == "must_review"),
            borderline=sum(1 for m in matches if m.category == "borderline"),
            probably_skip=sum(1 for m in matches if m.category == "probably_skip"),
        )
        return matches

    # ---- Capability 2: classify a single scored job (rule-based for M) ----

    def classify_match(self, scored_job: ScoredJob) -> str:
        """Return the review category for a single scored job."""
        if scored_job.score >= self.review_threshold:
            return "must_review"
        if scored_job.score >= self.borderline_threshold:
            return "borderline"
        return "probably_skip"

    # ---- Capability 3: draft outreach for a job ---------------------------

    def draft_outreach(
        self,
        job: Job,
        cv_signals: CVSignals,
        candidate_name: str,
        *,
        contact_name: Optional[str] = None,
        contact_role: Optional[str] = None,
        contact_email: Optional[str] = None,
        cv_excerpt: Optional[str] = None,
        voice_notes_md: Optional[str] = None,
        candidate_profile_md: Optional[str] = None,
        generator: Optional[OutreachGenerator] = None,
    ) -> tuple[OutreachDraft, int]:
        """Generate and persist an outreach draft for one job.

        Returns (draft, draft_id). On success, also:
        - saves the draft row in `outreach_drafts`
        - moves the application to OUTREACH_DRAFTED status
        - records a DRAFTED feedback event
        - persists contact info on the application if provided

        Raises:
            OutreachGenerationError: on any generation failure. State is not
            changed in that case.
        """
        gen = generator or OutreachGenerator()
        try:
            draft = gen.generate(
                job=job,
                cv=cv_signals,
                candidate_name=candidate_name,
                contact_name=contact_name,
                contact_role=contact_role,
                cv_excerpt=cv_excerpt,
                voice_notes_md=voice_notes_md,
                candidate_profile_md=candidate_profile_md,
            )
        except OutreachGenerationError:
            logger.warning(
                "agent_draft_outreach_failed",
                job_id=job.id,
                company=job.company,
            )
            raise

        draft_id = self.storage.save_draft(
            job_id=job.id,
            subject=draft.subject,
            body=draft.body,
            rationale=draft.rationale,
            self_rating=draft.self_rating,
        )

        self.storage.upsert_application(
            job.id,
            ApplicationStatus.OUTREACH_DRAFTED,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_role=contact_role,
        )

        self.storage.record_event(
            job.id,
            FeedbackAction.DRAFTED,
            metadata={"draft_id": draft_id, "self_rating": draft.self_rating},
        )

        return draft, draft_id

    # ---- Capability 4: surface stale follow-ups --------------------------

    def surface_followups(
        self,
        *,
        stale_days: Optional[int] = None,
    ) -> list[FollowupCandidate]:
        """Find open applications where contact has gone quiet too long.

        Looks at applications in OUTREACH_SENT or APPLIED status whose
        last_contact_at (or applied_at as fallback) is older than stale_days.
        """
        threshold_days = stale_days if stale_days is not None else self.followup_stale_days
        cutoff = datetime.utcnow() - timedelta(days=threshold_days)

        candidates: list[FollowupCandidate] = []
        for status in (ApplicationStatus.OUTREACH_SENT, ApplicationStatus.APPLIED):
            for row in self.storage.list_applications(status=status):
                last_contact_str = row.get("last_contact_at") or row.get("applied_at")
                if not last_contact_str:
                    continue
                try:
                    last_contact = datetime.fromisoformat(last_contact_str)
                except ValueError:
                    continue
                if last_contact > cutoff:
                    continue

                days_since = (datetime.utcnow() - last_contact).days
                job = self.storage.get_job_by_id(row["job_id"])
                candidates.append(
                    FollowupCandidate(
                        job_id=row["job_id"],
                        company=job.company if job else "(unknown)",
                        title=job.title if job else "(unknown)",
                        status=ApplicationStatus(row["status"]),
                        days_since_contact=days_since,
                        last_action=row["status"],
                        job=job,
                    )
                )

        # Sort by stalest first.
        candidates.sort(key=lambda c: -c.days_since_contact)
        return candidates

    # ---- Capability 5: record a user decision about a job ---------------

    def record_decision(
        self,
        job_id: str,
        action: FeedbackAction,
        *,
        reason: Optional[str] = None,
        notes: Optional[str] = None,
        company: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        """Record a user decision and propagate state.

        Side effects (in order):
        1. Append a feedback_event (audit log).
        2. Move the application to the new status (if action implies one).
        3. Mirror to the legacy feedback.db so existing scoring picks it up.

        `company` and `title` are required for the mirror step if the job
        isn't already in the main DB. Falls back to looking it up.
        """
        # 1. Audit log
        self.storage.record_event(job_id, action, reason=reason)

        # 2. Update application status
        new_status = self._action_to_status(action)
        if new_status is not None:
            kwargs = {}
            if action == FeedbackAction.APPLIED:
                kwargs["applied_at"] = datetime.utcnow()
                kwargs["last_contact_at"] = datetime.utcnow()
            elif action == FeedbackAction.SENT:
                kwargs["last_contact_at"] = datetime.utcnow()
            if notes is not None:
                kwargs["notes"] = notes
            self.storage.upsert_application(job_id, new_status, **kwargs)

        # 3. Mirror to feedback.db (best-effort; never raises)
        try:
            self._mirror_to_feedback_db(
                job_id=job_id,
                action=action,
                reason=reason,
                notes=notes,
                company=company,
                title=title,
            )
        except Exception as e:
            logger.warning(
                "agent_feedback_mirror_failed",
                job_id=job_id,
                action=action.value,
                error=str(e),
            )

    # ---- Capability 6: pipeline status overview --------------------------

    def pipeline_summary(self) -> dict:
        """Return counts by status plus recent activity."""
        counts = self.storage.get_pipeline_counts()
        active_count = sum(
            counts.get(s.value, 0) for s in ACTIVE_STATUSES
        )
        closed_count = sum(
            counts.get(s.value, 0) for s in CLOSED_STATUSES
        )
        return {
            "by_status": counts,
            "active": active_count,
            "closed": closed_count,
            "total": active_count + closed_count,
        }

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _action_to_status(action: FeedbackAction) -> Optional[ApplicationStatus]:
        """Map a feedback action to the application status it implies."""
        return {
            FeedbackAction.SAVED: ApplicationStatus.SAVED,
            FeedbackAction.SKIPPED: ApplicationStatus.SKIPPED,
            FeedbackAction.DRAFTED: ApplicationStatus.OUTREACH_DRAFTED,
            FeedbackAction.SENT: ApplicationStatus.OUTREACH_SENT,
            FeedbackAction.APPLIED: ApplicationStatus.APPLIED,
            FeedbackAction.REJECTED: ApplicationStatus.REJECTED,
            FeedbackAction.WITHDREW: ApplicationStatus.WITHDRAWN,
        }.get(action)

    def _mirror_to_feedback_db(
        self,
        *,
        job_id: str,
        action: FeedbackAction,
        reason: Optional[str],
        notes: Optional[str],
        company: Optional[str],
        title: Optional[str],
    ) -> None:
        """Write a row to the legacy feedback DB so existing scoring sees it.

        Mapping (intentionally lossy — feedback.db is binary like/dislike):
            SAVED       -> like
            SKIPPED     -> dislike (reason mapped if recognisable)
            APPLIED     -> applied=True (also like)
            REJECTED    -> dislike (reason mapped if recognisable)
            other       -> noop

        Resolves company/title from the main DB if not provided.
        """
        feedback_value: Optional[str] = None
        applied_flag = False

        if action == FeedbackAction.SAVED:
            feedback_value = "like"
        elif action == FeedbackAction.APPLIED:
            feedback_value = "like"
            applied_flag = True
        elif action == FeedbackAction.SKIPPED:
            feedback_value = "dislike"
        elif action == FeedbackAction.REJECTED:
            feedback_value = "dislike"
        else:
            return

        # The legacy schema only accepts these reason codes.
        valid_reasons = {"role", "company", "location", "seniority", "other"}
        dislike_reason = reason if reason in valid_reasons else None

        if company is None or title is None:
            job = self.storage.get_job_by_id(job_id)
            if job is not None:
                company = company or job.company
                title = title or job.title

        if company is None or title is None:
            logger.warning(
                "agent_feedback_mirror_skipped_unknown_job",
                job_id=job_id,
            )
            return

        self.feedback_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.feedback_db_path))
        try:
            cursor = conn.cursor()
            # Ensure the schema exists. The web UI creates it lazily; the
            # agent might run before the UI ever has.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS job_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    feedback TEXT NOT NULL CHECK(feedback IN ('like', 'dislike', 'neutral')),
                    dislike_reason TEXT CHECK(dislike_reason IN ('role', 'company', 'location', 'seniority', 'other', NULL)),
                    notes TEXT,
                    applied INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learned_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preference_type TEXT NOT NULL,
                    preference_key TEXT NOT NULL,
                    weight_adjustment REAL DEFAULT 0.0,
                    sample_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(preference_type, preference_key)
                )
            """)

            now = datetime.utcnow().isoformat()
            cursor.execute("""
                INSERT INTO job_feedback (job_id, company, title, feedback, dislike_reason, notes, applied, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    feedback = excluded.feedback,
                    dislike_reason = COALESCE(excluded.dislike_reason, job_feedback.dislike_reason),
                    notes = COALESCE(excluded.notes, job_feedback.notes),
                    applied = MAX(job_feedback.applied, excluded.applied),
                    updated_at = excluded.updated_at
            """, (
                job_id,
                company,
                title,
                feedback_value,
                dislike_reason,
                notes,
                1 if applied_flag else 0,
                now,
            ))

            # Update learned_preferences (mirrors web/app.py logic).
            self._update_learned_prefs(
                cursor,
                company=company,
                title=title,
                feedback=feedback_value,
                dislike_reason=dislike_reason,
            )

            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _update_learned_prefs(
        cursor: sqlite3.Cursor,
        *,
        company: str,
        title: str,
        feedback: str,
        dislike_reason: Optional[str],
    ) -> None:
        """Mirror of web/app.py update_learned_preferences().

        Kept here (rather than imported) so the agent doesn't depend on the
        Flask app being loadable. If the rules diverge between the two, the
        web UI is the source of truth — fix it there first, then reflect.
        """
        weight = 1.0 if feedback == "like" else (-1.0 if feedback == "dislike" else 0.0)
        if weight == 0.0:
            return

        now = datetime.utcnow().isoformat()
        learn_company = (feedback == "like") or (
            feedback == "dislike" and dislike_reason == "company"
        )
        if learn_company:
            cursor.execute("""
                INSERT INTO learned_preferences (preference_type, preference_key, weight_adjustment, sample_count, updated_at)
                VALUES ('company', ?, ?, 1, ?)
                ON CONFLICT(preference_type, preference_key) DO UPDATE SET
                    weight_adjustment = (learned_preferences.weight_adjustment * learned_preferences.sample_count + ?) / (learned_preferences.sample_count + 1),
                    sample_count = learned_preferences.sample_count + 1,
                    updated_at = ?
            """, (company, weight, now, weight, now))

        learn_role = (feedback == "like") or (
            feedback == "dislike" and dislike_reason in ("role", "seniority", None)
        )
        if not learn_role:
            return

        title_lower = title.lower()
        keywords: list[str] = []

        if "senior" in title_lower or "sr." in title_lower:
            keywords.append("seniority:senior")
        elif "staff" in title_lower or "principal" in title_lower:
            keywords.append("seniority:staff")
        elif "lead" in title_lower or "director" in title_lower:
            keywords.append("seniority:lead")
        else:
            keywords.append("seniority:mid")

        for domain in ["ai", "ml", "platform", "data", "growth", "infrastructure", "enterprise", "consumer"]:
            if domain in title_lower:
                keywords.append(f"domain:{domain}")

        for keyword in keywords:
            cursor.execute("""
                INSERT INTO learned_preferences (preference_type, preference_key, weight_adjustment, sample_count, updated_at)
                VALUES ('title_keyword', ?, ?, 1, ?)
                ON CONFLICT(preference_type, preference_key) DO UPDATE SET
                    weight_adjustment = (learned_preferences.weight_adjustment * learned_preferences.sample_count + ?) / (learned_preferences.sample_count + 1),
                    sample_count = learned_preferences.sample_count + 1,
                    updated_at = ?
            """, (keyword, weight, now, weight, now))
