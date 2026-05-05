"""Load Job objects from the JSON reports written by `role-radar run`.

The `prep` CLI command targets jobs from the latest report (or a specific
report file). This module rehydrates the JSON entries back into Job models so
the prep generator can consume them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from role_radar.models import ATSType, CompanyType, Job, JobLocation, SalaryInfo


def find_latest_report(outputs_dir: Path) -> Optional[Path]:
    """Return the path to the most recent report_*.json, or None."""
    if not outputs_dir.exists():
        return None
    candidates = sorted(outputs_dir.glob("report_*.json"), reverse=True)
    return candidates[0] if candidates else None


def load_report(path: Path) -> dict[str, Any]:
    """Load a report JSON file."""
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def job_from_report_entry(entry: dict[str, Any]) -> Job:
    """Rehydrate a Job from a single report entry."""
    job_data = entry["job"]
    loc_data = job_data.get("location", {})

    location = JobLocation(
        city=loc_data.get("city"),
        state=loc_data.get("state"),
        country=loc_data.get("country"),
        remote=bool(loc_data.get("remote", False)),
        hybrid=bool(loc_data.get("hybrid", False)),
        raw_location=loc_data.get("formatted") or "",
    )

    salary = None
    salary_data = job_data.get("salary")
    if salary_data:
        salary = SalaryInfo(
            min_salary=salary_data.get("min"),
            max_salary=salary_data.get("max"),
            currency=salary_data.get("currency", "USD"),
            is_estimated=bool(salary_data.get("is_estimated", False)),
        )

    posted_date = None
    if job_data.get("posted_date"):
        try:
            posted_date = datetime.fromisoformat(
                job_data["posted_date"].replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            posted_date = None

    company_type = CompanyType(job_data["company_type"]) if job_data.get("company_type") else CompanyType.AI_TOP_20
    source_ats = ATSType(job_data["source_ats"]) if job_data.get("source_ats") else ATSType.UNKNOWN

    return Job(
        id=job_data["id"],
        external_id=job_data.get("id", "").rsplit("_", 1)[-1],
        company=job_data["company"],
        company_slug=job_data["id"].split("_", 1)[0] if "_" in job_data["id"] else job_data["company"].lower(),
        company_type=company_type,
        title=job_data["title"],
        location=location,
        description=job_data.get("description"),
        apply_url=job_data["apply_url"],
        posted_date=posted_date,
        department=job_data.get("department"),
        seniority=job_data.get("seniority"),
        salary=salary,
        source_ats=source_ats,
    )


def find_jobs_in_report(
    report_data: dict[str, Any],
    job_id: Optional[str] = None,
    rank: Optional[int] = None,
    top_n: Optional[int] = None,
) -> list[tuple[int, Job]]:
    """Find one or more jobs in a report by id, rank, or top-N.

    Returns a list of (rank, job) tuples. Empty list if nothing matches.
    """
    entries = report_data.get("jobs", [])
    out: list[tuple[int, Job]] = []

    for entry in entries:
        entry_rank = entry.get("rank", 0)
        entry_id = entry.get("job", {}).get("id", "")

        if job_id and entry_id == job_id:
            out.append((entry_rank, job_from_report_entry(entry)))
        elif rank is not None and entry_rank == rank:
            out.append((entry_rank, job_from_report_entry(entry)))
        elif top_n is not None and entry_rank <= top_n:
            out.append((entry_rank, job_from_report_entry(entry)))

    return out
