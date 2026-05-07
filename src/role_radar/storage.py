"""SQLite storage for caching and persistence."""

import json
import sqlite3
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from role_radar.models import ATSType, Company, CompanyType, Job, JobLocation, RunSummary
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


class ApplicationStatus(str, Enum):
    """Pipeline status for a job. New states can be added as the agent grows."""
    NEW = "new"
    SAVED = "saved"
    OUTREACH_DRAFTED = "outreach_drafted"
    OUTREACH_SENT = "outreach_sent"
    APPLIED = "applied"
    RESPONDED = "responded"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    SKIPPED = "skipped"


class FeedbackAction(str, Enum):
    """Discrete actions the user takes on a job (audit log)."""
    SAVED = "saved"
    SKIPPED = "skipped"
    DRAFTED = "drafted"
    SENT = "sent"
    APPLIED = "applied"
    REJECTED = "rejected"
    WITHDREW = "withdrew"
    NOTED = "noted"
    RATED = "rated"


class Storage:
    """SQLite storage for Role Radar data."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Companies table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                company_type TEXT NOT NULL,
                homepage TEXT,
                careers_url TEXT,
                ats_type TEXT,
                ats_identifier TEXT,
                scraping_allowed INTEGER DEFAULT 1,
                ai_category TEXT,
                ai_score REAL,
                backed_by TEXT,
                notes TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Jobs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                external_id TEXT NOT NULL,
                company TEXT NOT NULL,
                company_slug TEXT NOT NULL,
                company_type TEXT NOT NULL,
                title TEXT NOT NULL,
                location_raw TEXT,
                location_city TEXT,
                location_state TEXT,
                location_country TEXT,
                location_remote INTEGER DEFAULT 0,
                location_hybrid INTEGER DEFAULT 0,
                description TEXT,
                apply_url TEXT NOT NULL,
                posted_date TEXT,
                department TEXT,
                employment_type TEXT,
                seniority TEXT,
                source_ats TEXT,
                fetched_at TEXT NOT NULL,
                raw_data TEXT,
                FOREIGN KEY (company_slug) REFERENCES companies(slug)
            )
        """)

        # Run history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS run_history (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                companies_processed INTEGER DEFAULT 0,
                companies_with_errors INTEGER DEFAULT 0,
                total_jobs_found INTEGER DEFAULT 0,
                jobs_after_filter INTEGER DEFAULT 0,
                jobs_after_dedupe INTEGER DEFAULT 0,
                jobs_scored INTEGER DEFAULT 0,
                jobs_in_email INTEGER DEFAULT 0,
                email_sent INTEGER DEFAULT 0,
                report_path TEXT,
                errors TEXT
            )
        """)

        # Applications — one row per job that's entered the agent's pipeline.
        # Status moves forward over time; columns nullable so L's send/contact
        # discovery can populate them later without a schema migration.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                user_rating INTEGER,
                contact_name TEXT,
                contact_email TEXT,
                contact_role TEXT,
                applied_at TEXT,
                last_contact_at TEXT,
                next_followup_at TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
        """)

        # Outreach drafts — many drafts per job possible (variants, retries).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS outreach_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                variant TEXT NOT NULL DEFAULT 'default',
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                rationale TEXT,
                self_rating INTEGER,
                drafted_at TEXT NOT NULL,
                sent INTEGER DEFAULT 0,
                sent_at TEXT,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
        """)

        # Feedback events — append-only audit log of every decision the user
        # makes through the agent. Distinct from web/app.py's job_feedback
        # which stores current like/dislike state.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                metadata TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
        """)

        # Create indexes
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_company_slug
            ON jobs(company_slug)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_fetched_at
            ON jobs(fetched_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_applications_status
            ON applications(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_outreach_drafts_job_id
            ON outreach_drafts(job_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_events_job_id
            ON feedback_events(job_id)
        """)

        conn.commit()
        logger.debug("database_initialized", path=str(self.db_path))

    def save_company(self, company: Company) -> None:
        """Save or update a company."""
        conn = self._get_conn()
        cursor = conn.cursor()

        backed_by_json = json.dumps(company.backed_by) if company.backed_by else None

        cursor.execute("""
            INSERT OR REPLACE INTO companies (
                slug, name, company_type, homepage, careers_url,
                ats_type, ats_identifier, scraping_allowed,
                ai_category, ai_score, backed_by, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company.slug,
            company.name,
            company.company_type.value,
            company.homepage,
            company.careers_url,
            company.ats_type.value if company.ats_type else None,
            company.ats_identifier,
            1 if company.scraping_allowed else 0,
            company.ai_category.value if company.ai_category else None,
            company.ai_score,
            backed_by_json,
            company.notes,
            datetime.utcnow().isoformat(),
        ))

        conn.commit()

    def get_company(self, slug: str) -> Optional[Company]:
        """Get a company by slug."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM companies WHERE slug = ?", (slug,))
        row = cursor.fetchone()

        if not row:
            return None

        backed_by = json.loads(row["backed_by"]) if row["backed_by"] else []

        return Company(
            name=row["name"],
            slug=row["slug"],
            company_type=CompanyType(row["company_type"]),
            homepage=row["homepage"],
            careers_url=row["careers_url"],
            ats_type=ATSType(row["ats_type"]) if row["ats_type"] else ATSType.UNKNOWN,
            ats_identifier=row["ats_identifier"],
            scraping_allowed=bool(row["scraping_allowed"]),
            ai_category=row["ai_category"],
            ai_score=row["ai_score"],
            backed_by=backed_by,
            notes=row["notes"],
        )

    def get_all_companies(self, company_type: Optional[CompanyType] = None) -> list[Company]:
        """Get all companies, optionally filtered by type."""
        conn = self._get_conn()
        cursor = conn.cursor()

        if company_type:
            cursor.execute(
                "SELECT * FROM companies WHERE company_type = ?",
                (company_type.value,)
            )
        else:
            cursor.execute("SELECT * FROM companies")

        companies = []
        for row in cursor.fetchall():
            backed_by = json.loads(row["backed_by"]) if row["backed_by"] else []
            company = Company(
                name=row["name"],
                slug=row["slug"],
                company_type=CompanyType(row["company_type"]),
                homepage=row["homepage"],
                careers_url=row["careers_url"],
                ats_type=ATSType(row["ats_type"]) if row["ats_type"] else ATSType.UNKNOWN,
                ats_identifier=row["ats_identifier"],
                scraping_allowed=bool(row["scraping_allowed"]),
                ai_category=row["ai_category"],
                ai_score=row["ai_score"],
                backed_by=backed_by,
                notes=row["notes"],
            )
            companies.append(company)

        return companies

    def save_job(self, job: Job) -> None:
        """Save or update a job."""
        conn = self._get_conn()
        cursor = conn.cursor()

        raw_data_json = json.dumps(job.raw_data) if job.raw_data else None

        cursor.execute("""
            INSERT OR REPLACE INTO jobs (
                id, external_id, company, company_slug, company_type,
                title, location_raw, location_city, location_state,
                location_country, location_remote, location_hybrid,
                description, apply_url, posted_date, department,
                employment_type, seniority, source_ats, fetched_at, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job.id,
            job.external_id,
            job.company,
            job.company_slug,
            job.company_type.value,
            job.title,
            job.location.raw_location,
            job.location.city,
            job.location.state,
            job.location.country,
            1 if job.location.remote else 0,
            1 if job.location.hybrid else 0,
            job.description,
            job.apply_url,
            job.posted_date.isoformat() if job.posted_date else None,
            job.department,
            job.employment_type,
            job.seniority,
            job.source_ats.value if job.source_ats else None,
            job.fetched_at.isoformat(),
            raw_data_json,
        ))

        conn.commit()

    def save_jobs(self, jobs: list[Job]) -> None:
        """Save multiple jobs."""
        for job in jobs:
            self.save_job(job)

    def get_jobs(
        self,
        company_slug: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list[Job]:
        """Get jobs, optionally filtered."""
        conn = self._get_conn()
        cursor = conn.cursor()

        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []

        if company_slug:
            query += " AND company_slug = ?"
            params.append(company_slug)

        if since:
            query += " AND fetched_at >= ?"
            params.append(since.isoformat())

        cursor.execute(query, params)

        jobs = []
        for row in cursor.fetchall():
            location = JobLocation(
                city=row["location_city"],
                state=row["location_state"],
                country=row["location_country"],
                remote=bool(row["location_remote"]),
                hybrid=bool(row["location_hybrid"]),
                raw_location=row["location_raw"] or "",
            )

            posted_date = None
            if row["posted_date"]:
                try:
                    posted_date = datetime.fromisoformat(row["posted_date"])
                except ValueError:
                    pass

            raw_data = None
            if row["raw_data"]:
                try:
                    raw_data = json.loads(row["raw_data"])
                except json.JSONDecodeError:
                    pass

            job = Job(
                id=row["id"],
                external_id=row["external_id"],
                company=row["company"],
                company_slug=row["company_slug"],
                company_type=CompanyType(row["company_type"]),
                title=row["title"],
                location=location,
                description=row["description"],
                apply_url=row["apply_url"],
                posted_date=posted_date,
                department=row["department"],
                employment_type=row["employment_type"],
                seniority=row["seniority"],
                source_ats=ATSType(row["source_ats"]) if row["source_ats"] else ATSType.UNKNOWN,
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
                raw_data=raw_data,
            )
            jobs.append(job)

        return jobs

    def get_cached_jobs(self, max_age_hours: int = 24) -> list[Job]:
        """Get jobs cached within the specified time window."""
        since = datetime.utcnow() - timedelta(hours=max_age_hours)
        return self.get_jobs(since=since)

    def clear_old_jobs(self, max_age_days: int = 30) -> int:
        """Remove jobs older than the specified age."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
        cursor.execute("DELETE FROM jobs WHERE fetched_at < ?", (cutoff,))
        deleted = cursor.rowcount

        conn.commit()
        logger.info("old_jobs_cleared", deleted=deleted, max_age_days=max_age_days)

        return deleted

    def save_run_summary(self, summary: RunSummary) -> None:
        """Save a run summary."""
        conn = self._get_conn()
        cursor = conn.cursor()

        errors_json = json.dumps(summary.errors) if summary.errors else None

        cursor.execute("""
            INSERT OR REPLACE INTO run_history (
                run_id, started_at, completed_at,
                companies_processed, companies_with_errors,
                total_jobs_found, jobs_after_filter, jobs_after_dedupe,
                jobs_scored, jobs_in_email, email_sent, report_path, errors
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            summary.run_id,
            summary.started_at.isoformat(),
            summary.completed_at.isoformat() if summary.completed_at else None,
            summary.companies_processed,
            summary.companies_with_errors,
            summary.total_jobs_found,
            summary.jobs_after_filter,
            summary.jobs_after_dedupe,
            summary.jobs_scored,
            summary.jobs_in_email,
            1 if summary.email_sent else 0,
            summary.report_path,
            errors_json,
        ))

        conn.commit()

    def get_last_run(self) -> Optional[RunSummary]:
        """Get the most recent run summary."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM run_history
            ORDER BY started_at DESC
            LIMIT 1
        """)
        row = cursor.fetchone()

        if not row:
            return None

        return RunSummary(
            run_id=row["run_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            companies_processed=row["companies_processed"],
            companies_with_errors=row["companies_with_errors"],
            total_jobs_found=row["total_jobs_found"],
            jobs_after_filter=row["jobs_after_filter"],
            jobs_after_dedupe=row["jobs_after_dedupe"],
            jobs_scored=row["jobs_scored"],
            jobs_in_email=row["jobs_in_email"],
            email_sent=bool(row["email_sent"]),
            report_path=row["report_path"],
            errors=json.loads(row["errors"]) if row["errors"] else [],
        )

    # ---- Agent: applications, drafts, feedback events ---------------------

    def get_application(self, job_id: str) -> Optional[dict]:
        """Return the application row for a job, or None if not in the pipeline."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_application(
        self,
        job_id: str,
        status: ApplicationStatus,
        *,
        user_rating: Optional[int] = None,
        contact_name: Optional[str] = None,
        contact_email: Optional[str] = None,
        contact_role: Optional[str] = None,
        applied_at: Optional[datetime] = None,
        last_contact_at: Optional[datetime] = None,
        next_followup_at: Optional[datetime] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Create or update an application row.

        Only non-None fields are written on update — pass None to leave a column
        unchanged. To clear a field, write a sentinel like '' instead.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        existing = self.get_application(job_id)

        if existing is None:
            cursor.execute("""
                INSERT INTO applications (
                    job_id, status, user_rating, contact_name, contact_email,
                    contact_role, applied_at, last_contact_at, next_followup_at,
                    notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id,
                status.value,
                user_rating,
                contact_name,
                contact_email,
                contact_role,
                applied_at.isoformat() if applied_at else None,
                last_contact_at.isoformat() if last_contact_at else None,
                next_followup_at.isoformat() if next_followup_at else None,
                notes,
                now,
                now,
            ))
        else:
            updates: list[tuple[str, object]] = [("status", status.value)]
            if user_rating is not None:
                updates.append(("user_rating", user_rating))
            if contact_name is not None:
                updates.append(("contact_name", contact_name))
            if contact_email is not None:
                updates.append(("contact_email", contact_email))
            if contact_role is not None:
                updates.append(("contact_role", contact_role))
            if applied_at is not None:
                updates.append(("applied_at", applied_at.isoformat()))
            if last_contact_at is not None:
                updates.append(("last_contact_at", last_contact_at.isoformat()))
            if next_followup_at is not None:
                updates.append(("next_followup_at", next_followup_at.isoformat()))
            if notes is not None:
                updates.append(("notes", notes))
            updates.append(("updated_at", now))

            set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
            params = [val for _, val in updates] + [job_id]
            cursor.execute(
                f"UPDATE applications SET {set_clause} WHERE job_id = ?",
                params,
            )

        conn.commit()

    def list_applications(
        self,
        *,
        status: Optional[ApplicationStatus] = None,
    ) -> list[dict]:
        """List applications, optionally filtered by status. Newest first."""
        conn = self._get_conn()
        cursor = conn.cursor()

        if status is not None:
            cursor.execute(
                "SELECT * FROM applications WHERE status = ? ORDER BY updated_at DESC",
                (status.value,),
            )
        else:
            cursor.execute("SELECT * FROM applications ORDER BY updated_at DESC")

        return [dict(row) for row in cursor.fetchall()]

    def get_pipeline_counts(self) -> dict[str, int]:
        """Return {status: count} across all applications."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, COUNT(*) FROM applications GROUP BY status"
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    def save_draft(
        self,
        job_id: str,
        subject: str,
        body: str,
        *,
        variant: str = "default",
        rationale: Optional[str] = None,
        self_rating: Optional[int] = None,
    ) -> int:
        """Save an outreach draft. Returns the draft id."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO outreach_drafts (
                job_id, variant, subject, body, rationale, self_rating, drafted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id,
            variant,
            subject,
            body,
            rationale,
            self_rating,
            datetime.utcnow().isoformat(),
        ))
        conn.commit()
        return cursor.lastrowid

    def get_drafts(self, job_id: str) -> list[dict]:
        """Get all drafts for a job, newest first.

        Orders by id DESC so two drafts written in the same millisecond
        still come back in insertion order (drafted_at is second-precision).
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM outreach_drafts WHERE job_id = ? ORDER BY id DESC",
            (job_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def mark_draft_sent(self, draft_id: int) -> None:
        """Mark a draft as sent. (Used by L; safe to call now.)"""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE outreach_drafts SET sent = 1, sent_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), draft_id),
        )
        conn.commit()

    def record_event(
        self,
        job_id: str,
        action: FeedbackAction,
        *,
        reason: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Append a feedback event to the audit log."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO feedback_events (job_id, action, reason, metadata, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            job_id,
            action.value,
            reason,
            json.dumps(metadata) if metadata else None,
            datetime.utcnow().isoformat(),
        ))
        conn.commit()

    def get_events(self, job_id: str) -> list[dict]:
        """Get all feedback events for a job, oldest first (audit order)."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM feedback_events WHERE job_id = ? ORDER BY created_at ASC",
            (job_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_job_by_id(self, job_id: str) -> Optional[Job]:
        """Fetch a single job by its id."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        if not row:
            return None

        location = JobLocation(
            city=row["location_city"],
            state=row["location_state"],
            country=row["location_country"],
            remote=bool(row["location_remote"]),
            hybrid=bool(row["location_hybrid"]),
            raw_location=row["location_raw"] or "",
        )

        posted_date = None
        if row["posted_date"]:
            try:
                posted_date = datetime.fromisoformat(row["posted_date"])
            except ValueError:
                pass

        raw_data = None
        if row["raw_data"]:
            try:
                raw_data = json.loads(row["raw_data"])
            except json.JSONDecodeError:
                pass

        return Job(
            id=row["id"],
            external_id=row["external_id"],
            company=row["company"],
            company_slug=row["company_slug"],
            company_type=CompanyType(row["company_type"]),
            title=row["title"],
            location=location,
            description=row["description"],
            apply_url=row["apply_url"],
            posted_date=posted_date,
            department=row["department"],
            employment_type=row["employment_type"],
            seniority=row["seniority"],
            source_ats=ATSType(row["source_ats"]) if row["source_ats"] else ATSType.UNKNOWN,
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            raw_data=raw_data,
        )

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
