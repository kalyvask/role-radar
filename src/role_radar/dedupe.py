"""Job deduplication logic."""

import re
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse

from role_radar.models import Job
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


def normalize_title(title: str) -> str:
    """Normalize a job title for comparison."""
    # Lowercase
    title = title.lower()

    # Remove common prefixes/suffixes
    title = re.sub(r"^\s*\d+\s*-?\s*", "", title)  # Leading numbers
    title = re.sub(r"\s*-\s*\d+\s*$", "", title)   # Trailing numbers
    title = re.sub(r"\s+", " ", title)             # Multiple spaces

    # Normalize common abbreviations. Match the abbreviation as a whole word,
    # then consume an optional trailing period. The period must come AFTER the
    # closing \b: in `\bsr\.?\b`, for "sr. " the `\b` can't sit between "." and
    # a space, so the period is left orphaned ("senior. pm"). `\bsr\b\.?` fixes it.
    replacements = [
        (r"\bsr\b\.?", "senior"),
        (r"\bjr\b\.?", "junior"),
        (r"\bmgr\b\.?", "manager"),
        (r"\beng\b\.?", "engineer"),
        (r"\bprod\b\.?", "product"),
    ]
    for pattern, replacement in replacements:
        title = re.sub(pattern, replacement, title)

    return title.strip()


def normalize_company(company: str) -> str:
    """Normalize a company name for comparison."""
    company = company.lower()

    # Remove common legal suffixes. Each must be a separate trailing token —
    # preceded by a comma or whitespace — so we strip ", Inc" / " Corp" but not
    # the "corp" inside "TechCorp" (which previously collapsed to "tech").
    suffixes = [
        r"(?:,\s*|\s+)inc\.?$",
        r"(?:,\s*|\s+)llc\.?$",
        r"(?:,\s*|\s+)ltd\.?$",
        r"(?:,\s*|\s+)corp\.?$",
        r"(?:,\s*|\s+)co\.?$",
    ]
    for suffix in suffixes:
        company = re.sub(suffix, "", company, flags=re.IGNORECASE)

    return company.strip()


def get_url_signature(url: str) -> str:
    """Get a signature from a URL for comparison."""
    parsed = urlparse(url)
    # Include domain and path, ignore query params
    return f"{parsed.netloc}{parsed.path}".lower()


def title_similarity(title1: str, title2: str) -> float:
    """Calculate similarity between two titles."""
    t1 = normalize_title(title1)
    t2 = normalize_title(title2)
    return SequenceMatcher(None, t1, t2).ratio()


def is_duplicate(job1: Job, job2: Job, title_threshold: float = 0.85) -> bool:
    """Check if two jobs are duplicates.

    Two jobs are considered duplicates if:
    1. Same company (normalized) AND very similar title
    2. Same apply URL
    3. Same external ID and company
    """
    # Check exact URL match
    sig1 = get_url_signature(job1.apply_url)
    sig2 = get_url_signature(job2.apply_url)
    if sig1 and sig2 and sig1 == sig2:
        return True

    # Check same company + similar title
    company1 = normalize_company(job1.company)
    company2 = normalize_company(job2.company)

    if company1 == company2:
        # Same company, check title similarity
        similarity = title_similarity(job1.title, job2.title)
        if similarity >= title_threshold:
            return True

    return False


def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """Remove duplicate job postings.

    Returns a deduplicated list, preferring:
    1. Jobs with more complete data (description, posted date)
    2. Earlier occurrences for otherwise equal jobs
    """
    if not jobs:
        return []

    def job_completeness(job: Job) -> int:
        """Score how complete a job's data is."""
        score = 0
        if job.description:
            score += 3
        if job.posted_date:
            score += 2
        if job.department:
            score += 1
        if job.seniority:
            score += 1
        if job.location.city:
            score += 1
        return score

    # Sort by completeness (descending) so we keep the most complete version
    sorted_jobs = sorted(jobs, key=lambda j: -job_completeness(j))

    unique_jobs: list[Job] = []
    seen_signatures: set[str] = set()

    for job in sorted_jobs:
        # Quick check using URL signature
        sig = get_url_signature(job.apply_url)
        if sig in seen_signatures:
            continue

        # Check against all unique jobs for duplicates
        is_dupe = False
        for existing in unique_jobs:
            if is_duplicate(job, existing):
                is_dupe = True
                break

        if not is_dupe:
            unique_jobs.append(job)
            if sig:
                seen_signatures.add(sig)

    logger.info(
        "jobs_deduplicated",
        input_count=len(jobs),
        output_count=len(unique_jobs),
        removed=len(jobs) - len(unique_jobs),
    )

    return unique_jobs
