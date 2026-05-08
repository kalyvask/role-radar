"""Workday ATS connector.

Workday hosts company career sites at:
    https://{tenant}.{pod}.myworkdayjobs.com/{board}

The unauthenticated job search API lives at:
    POST https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs

with a JSON body of:
    {"appliedFacets": {}, "limit": 20, "offset": <N>, "searchText": ""}

Each response page returns up to 20 job postings plus a `total` count, so
this connector paginates with `offset += 20` until all jobs are fetched.

Tenant identifiers are stored as `ats_identifier` in the form
`tenant:pod:board`, e.g. `crowdstrike:wd5:crowdstrikecareers`.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from role_radar.connectors.base import BaseConnector
from role_radar.models import ATSType, Company, Job, JobLocation, SalaryInfo
from role_radar.utils.http import HTTPClient
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


# Workday returns up to ~20 postings per page regardless of higher requested limit.
PAGE_SIZE = 20

# Cap pagination so a misconfigured tenant can't cause an infinite loop.
MAX_PAGES = 60


def parse_workday_identifier(identifier: str) -> Optional[tuple[str, str, str]]:
    """Parse a `tenant:pod:board` identifier into its three pieces.

    Returns (tenant, pod, board) or None if malformed.
    """
    if not identifier:
        return None
    parts = identifier.split(":")
    if len(parts) != 3:
        return None
    tenant, pod, board = (p.strip() for p in parts)
    if not tenant or not pod or not board:
        return None
    return tenant, pod, board


class WorkdayConnector(BaseConnector):
    """Connector for Workday-hosted careers sites."""

    def __init__(self, http_client: HTTPClient):
        super().__init__(http_client)

    def _api_url(self, tenant: str, pod: str, board: str) -> str:
        return f"https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"

    def _site_root(self, tenant: str, pod: str) -> str:
        return f"https://{tenant}.{pod}.myworkdayjobs.com"

    def _parse_posted_date(self, posted_on: Optional[str]) -> Optional[datetime]:
        """Parse Workday's relative `postedOn` strings (e.g. 'Posted 3 Days Ago')."""
        if not posted_on:
            return None
        text = posted_on.lower()
        now = datetime.now(timezone.utc)
        if "today" in text or "just posted" in text:
            return now
        if "yesterday" in text:
            return now - timedelta(days=1)

        days_match = re.search(r"(\d+)\s*\+?\s*day", text)
        if days_match:
            return now - timedelta(days=int(days_match.group(1)))

        weeks_match = re.search(r"(\d+)\s*\+?\s*week", text)
        if weeks_match:
            return now - timedelta(weeks=int(weeks_match.group(1)))

        months_match = re.search(r"(\d+)\s*\+?\s*month", text)
        if months_match:
            return now - timedelta(days=30 * int(months_match.group(1)))

        return None

    def _parse_location(self, posting: dict) -> JobLocation:
        raw = posting.get("locationsText") or ""
        if isinstance(raw, list):
            raw = ", ".join(raw)

        raw_lower = raw.lower()
        remote = "remote" in raw_lower
        hybrid = "hybrid" in raw_lower

        # Workday "locationsText" is often "USA - Sunnyvale, CA" or
        # "3 Locations" (multi-location postings). Try to extract a useful
        # city/state when present.
        city = None
        state = None
        country = None

        if " - " in raw:
            tail = raw.split(" - ", 1)[1]
            country = raw.split(" - ", 1)[0].strip()
        else:
            tail = raw

        parts = [p.strip() for p in tail.split(",")]
        if len(parts) >= 1 and parts[0]:
            city = parts[0]
        if len(parts) >= 2 and parts[1]:
            state = parts[1]

        return JobLocation(
            city=city,
            state=state,
            country=country,
            remote=remote,
            hybrid=hybrid,
            raw_location=raw,
        )

    def _parse_salary(self, posting: dict) -> Optional[SalaryInfo]:
        """Workday rarely exposes salary in the search API; check bullet fields."""
        bullets = posting.get("bulletFields") or []
        for bullet in bullets:
            if not isinstance(bullet, str):
                continue
            match = re.search(
                r"\$\s*([\d,]+)(?:\s*[—–-]\s*\$\s*([\d,]+))?",
                bullet,
            )
            if match:
                try:
                    min_s = int(match.group(1).replace(",", ""))
                    max_s = int(match.group(2).replace(",", "")) if match.group(2) else None
                    if min_s >= 30000 and (max_s is None or max_s <= 2000000):
                        return SalaryInfo(
                            min_salary=min_s,
                            max_salary=max_s,
                            currency="USD",
                            interval="year",
                            is_estimated=False,
                        )
                except (ValueError, IndexError):
                    pass
        return None

    def _parse_job(
        self, posting: dict, company: Company, site_root: str
    ) -> Job:
        external_path = posting.get("externalPath", "") or ""
        # Workday external IDs come from the trailing path segment, which is
        # usually unique and stable across pages.
        external_id = external_path.rstrip("/").rsplit("/", 1)[-1] or posting.get("title", "")
        title = posting.get("title", "")
        apply_url = (
            f"{site_root}{external_path}"
            if external_path.startswith("/")
            else (external_path or site_root)
        )
        posted_date = self._parse_posted_date(posting.get("postedOn"))
        location = self._parse_location(posting)
        salary = self._parse_salary(posting)

        return Job(
            id=f"{company.slug}_{external_id}",
            external_id=external_id,
            company=company.name,
            company_slug=company.slug,
            company_type=company.company_type,
            title=title,
            location=location,
            description=None,  # search API only returns metadata; no description
            apply_url=apply_url,
            posted_date=posted_date,
            department=None,
            salary=salary,
            source_ats=ATSType.WORKDAY,
            raw_data=posting,
        )

    def _fetch_page(self, url: str, offset: int) -> dict:
        body = {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        return self.http_client.post_json(url, json_body=body)

    def fetch_jobs(self, company: Company) -> list[Job]:
        parsed = parse_workday_identifier(company.ats_identifier or "")
        if not parsed:
            logger.warning(
                "workday_invalid_identifier",
                company=company.name,
                ats_identifier=company.ats_identifier,
                hint="Use 'tenant:pod:board' (e.g. 'crowdstrike:wd5:crowdstrikecareers')",
            )
            return []

        tenant, pod, board = parsed
        url = self._api_url(tenant, pod, board)
        site_root = self._site_root(tenant, pod)

        jobs: list[Job] = []
        offset = 0
        total: Optional[int] = None
        seen_ids: set[str] = set()

        for page in range(MAX_PAGES):
            try:
                data = self._fetch_page(url, offset)
            except Exception as e:
                logger.error(
                    "workday_fetch_error",
                    company=company.name,
                    tenant=tenant,
                    page=page,
                    offset=offset,
                    error=str(e),
                )
                break

            postings = data.get("jobPostings") or []
            if not postings:
                break

            if total is None:
                total = data.get("total")

            new_in_page = 0
            for posting in postings:
                try:
                    job = self._parse_job(posting, company, site_root)
                except Exception as e:
                    logger.warning(
                        "workday_parse_error",
                        company=company.name,
                        error=str(e),
                    )
                    continue
                if job.external_id in seen_ids:
                    continue
                seen_ids.add(job.external_id)
                jobs.append(job)
                new_in_page += 1

            # Stop conditions: known total reached, or no new IDs in this page.
            if total is not None and len(jobs) >= total:
                break
            if new_in_page == 0:
                break

            offset += PAGE_SIZE

        logger.info(
            "workday_jobs_fetched",
            company=company.name,
            tenant=tenant,
            pod=pod,
            board=board,
            job_count=len(jobs),
            reported_total=total,
        )
        return jobs

    def get_raw_data(self, company: Company) -> Optional[dict]:
        parsed = parse_workday_identifier(company.ats_identifier or "")
        if not parsed:
            return None
        tenant, pod, board = parsed
        url = self._api_url(tenant, pod, board)
        try:
            return self._fetch_page(url, offset=0)
        except Exception as e:
            logger.error("workday_raw_fetch_error", error=str(e))
            return None
