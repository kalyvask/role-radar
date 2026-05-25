"""Job scoring and matching against CV signals."""

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from role_radar.config import Preferences
from role_radar.models import (
    CompanyType,
    CVSignals,
    Job,
    ScoreBreakdown,
    ScoredJob,
)
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


def load_learned_preferences() -> dict:
    """Load learned preferences from feedback database."""
    db_path = Path.home() / ".role_radar" / "feedback.db"

    if not db_path.exists():
        return {"company": {}, "title_keyword": {}}

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            SELECT preference_type, preference_key, weight_adjustment, sample_count
            FROM learned_preferences
            WHERE sample_count >= 1
        """)
        rows = cursor.fetchall()
        conn.close()

        preferences = {"company": {}, "title_keyword": {}}
        for row in rows:
            ptype, pkey, weight, count = row
            if ptype in preferences:
                preferences[ptype][pkey] = {"weight": weight, "count": count}

        return preferences
    except Exception as e:
        logger.warning("failed_to_load_learned_preferences", error=str(e))
        return {"company": {}, "title_keyword": {}}


# Seniority levels with approximate years of experience
SENIORITY_LEVELS = {
    "APM": 0,
    "PM": 2,
    "Senior PM": 5,
    "Staff PM": 7,
    "Principal PM": 8,
    "Group PM": 8,
    "Director": 10,
    "VP": 12,
}

# Title patterns for seniority detection
TITLE_SENIORITY_PATTERNS = [
    (r"\b(vp|vice\s+president)\b", "VP"),
    (r"\bdirector\b", "Director"),
    (r"\b(group\s+pm|group\s+product)\b", "Group PM"),
    (r"\b(principal|staff)\s*(pm|product)", "Principal PM"),
    (r"\b(senior|sr\.?)\s*(pm|product)", "Senior PM"),
    (r"\b(associate|apm)\b", "APM"),
    (r"\b(pm|product\s+manager)\b", "PM"),
]


def detect_job_seniority(title: str) -> Optional[str]:
    """Detect seniority level from a job title."""
    title_lower = title.lower()

    for pattern, level in TITLE_SENIORITY_PATTERNS:
        if re.search(pattern, title_lower):
            return level

    return None


def is_pm_title(title: str, allowed_titles: list[str], include_rules=None) -> bool:
    """Check if a title matches PM-related patterns.

    If `include_rules` is provided and non-empty, the title matches when ANY rule
    matches (composable AND/OR semantics). Otherwise falls back to substring
    matching against `allowed_titles` plus generic PM regex patterns.
    """
    if include_rules:
        return any(rule.matches(title) for rule in include_rules)

    title_lower = title.lower()

    # Check against allowed titles
    for allowed in allowed_titles:
        if allowed.lower() in title_lower:
            return True

    # Fallback patterns
    pm_patterns = [
        r"\bproduct\s*manager\b",
        r"\bpm\b",
        r"\bproduct\s*lead\b",
        r"\bproduct\s*director\b",
        r"\bvp\s*(?:of\s+)?product\b",
        r"\bhead\s+of\s+product\b",
    ]

    return any(re.search(p, title_lower) for p in pm_patterns)


def should_exclude_job(job: Job, excluded_keywords: list[str], exclude_rules=None) -> bool:
    """Check if a job should be excluded.

    If `exclude_rules` is provided and non-empty, the job is excluded when ANY rule
    matches against `title + " " + department`. Otherwise falls back to substring
    matching against `excluded_keywords`.
    """
    title_lower = job.title.lower()
    dept_lower = (job.department or "").lower()
    combined = f"{title_lower} {dept_lower}"

    if exclude_rules:
        return any(rule.matches(combined) for rule in exclude_rules)

    for keyword in excluded_keywords:
        kw_lower = keyword.lower()
        if kw_lower in title_lower or kw_lower in dept_lower:
            return True

    return False


def has_required_keywords(job: Job, required_keywords: list[str]) -> bool:
    """Check if a job has at least one required keyword."""
    if not required_keywords:
        return True

    title_lower = job.title.lower()
    dept_lower = (job.department or "").lower()
    desc_lower = (job.description or "").lower()

    combined = f"{title_lower} {dept_lower} {desc_lower}"

    for keyword in required_keywords:
        if keyword.lower() in combined:
            return True

    return False


def _matches_single_location(job: Job, target_location: str) -> bool:
    """Check if a job matches one target location string."""
    target_lower = target_location.lower()
    location_lower = job.location.raw_location.lower()

    # San Francisco Bay Area
    if "san francisco" in target_lower or "bay area" in target_lower:
        return job.location.is_sf_bay_area()

    # New York metro
    if (
        "new york" in target_lower
        or target_lower.strip() in {"nyc", "ny"}
        or "manhattan" in target_lower
    ):
        return job.location.is_nyc()

    # Generic location matching
    return target_lower in location_lower


def matches_location(
    job: Job,
    target_location: Union[str, list[str]],
    include_remote: bool,
) -> bool:
    """Check if job matches location preferences.

    `target_location` may be a single string or a list of strings. A job matches
    if it satisfies ANY of the targets.
    """
    # Remote jobs match if remote is allowed
    if job.location.remote and include_remote:
        return True

    targets = [target_location] if isinstance(target_location, str) else list(target_location)
    return any(_matches_single_location(job, t) for t in targets)


class JobScorer:
    """Scores jobs against CV signals."""

    def __init__(
        self,
        cv_signals: CVSignals,
        preferences: Preferences,
        use_learned_preferences: bool = True,
    ):
        self.cv_signals = cv_signals
        self.preferences = preferences
        self.learned_prefs = load_learned_preferences() if use_learned_preferences else {}

        if self.learned_prefs.get("company") or self.learned_prefs.get("title_keyword"):
            logger.info(
                "loaded_learned_preferences",
                company_prefs=len(self.learned_prefs.get("company", {})),
                keyword_prefs=len(self.learned_prefs.get("title_keyword", {})),
            )

    def _score_title_seniority(self, job: Job) -> tuple[float, str]:
        """Score title/seniority match (0-25 points)."""
        job_seniority = detect_job_seniority(job.title)
        cv_seniority = self.cv_signals.inferred_seniority

        if not job_seniority:
            return 15.0, "Could not detect job seniority"

        if not cv_seniority:
            return 15.0, f"Job is {job_seniority} level"

        job_level = SENIORITY_LEVELS.get(job_seniority, 3)
        cv_level = SENIORITY_LEVELS.get(cv_seniority, 3)

        # Calculate match - penalize large gaps aggressively to avoid over-senior false positives
        level_diff = abs(job_level - cv_level)
        # Direction matters: a job *more senior* than CV is worse than a job slightly junior
        too_senior = job_level > cv_level

        if level_diff == 0:
            score = 25.0
            detail = f"Exact seniority match ({job_seniority})"
        elif level_diff == 1:
            score = 22.0
            detail = f"Close seniority match: Job {job_seniority}, CV suggests {cv_seniority}"
        elif level_diff == 2:
            score = 16.0 if too_senior else 18.0
            detail = f"Moderate gap: Job {job_seniority}, CV suggests {cv_seniority}"
        elif level_diff == 3:
            score = 6.0 if too_senior else 12.0
            detail = f"Large gap: Job {job_seniority}, CV suggests {cv_seniority}"
        else:
            score = 0.0 if too_senior else 8.0
            detail = f"Mismatch: Job {job_seniority} too senior for CV ({cv_seniority})"

        # Bonus for preferred seniority
        if job_seniority in self.preferences.seniority:
            score = min(25.0, score + 2.0)

        # Extra boost for explicit AI PM titles
        title_lower = job.title.lower()
        if "ai product manager" in title_lower or "ai pm" in title_lower:
            score = min(25.0, score + 3.0)
            detail += " [AI PM boost]"

        return score, detail

    def _score_skills_overlap(self, job: Job) -> tuple[float, list[str]]:
        """Score skills overlap (0-35 points)."""
        cv_skills = set(s.lower() for s in self.cv_signals.skills)

        if not cv_skills:
            return 20.0, []

        # Get job text to match against
        job_text = f"{job.title} {job.department or ''} {job.description or ''}"
        job_text_lower = job_text.lower()

        matched_skills = []
        for skill in cv_skills:
            if skill in job_text_lower:
                matched_skills.append(skill)

        # Calculate score based on overlap percentage
        if not matched_skills:
            return 10.0, []

        overlap_ratio = len(matched_skills) / len(cv_skills)

        if overlap_ratio >= 0.5:
            score = 35.0
        elif overlap_ratio >= 0.3:
            score = 28.0
        elif overlap_ratio >= 0.2:
            score = 22.0
        elif overlap_ratio >= 0.1:
            score = 16.0
        else:
            score = 12.0

        return score, matched_skills

    def _score_domain_overlap(self, job: Job) -> tuple[float, list[str]]:
        """Score domain expertise overlap (0-25 points)."""
        cv_domains = set(d.lower() for d in self.cv_signals.domains)

        if not cv_domains:
            return 15.0, []

        # Get job text to match against
        job_text = f"{job.title} {job.department or ''} {job.description or ''} {job.company}"
        job_text_lower = job_text.lower()

        # Domain keywords to check
        domain_keywords = {
            "ai/ml": ["ai", "ml", "machine learning", "artificial intelligence", "llm"],
            "infrastructure": ["infrastructure", "infra", "platform", "cloud", "devops"],
            "developer tools": ["developer", "devtools", "sdk", "api"],
            "b2b saas": ["b2b", "saas", "enterprise"],
            "fintech": ["fintech", "financial", "payments", "banking"],
            "consumer": ["consumer", "b2c", "social", "mobile"],
            "healthcare": ["healthcare", "health", "medical"],
            "e-commerce": ["ecommerce", "e-commerce", "marketplace", "retail"],
            "security": ["security", "cybersecurity"],
            "data": ["data", "analytics", "data platform"],
        }

        matched_domains = []
        for domain in cv_domains:
            keywords = domain_keywords.get(domain, [domain])
            for kw in keywords:
                if kw in job_text_lower:
                    matched_domains.append(domain)
                    break

        if not matched_domains:
            return 12.0, []

        overlap_ratio = len(matched_domains) / len(cv_domains)

        if overlap_ratio >= 0.5:
            score = 25.0
        elif overlap_ratio >= 0.3:
            score = 20.0
        elif overlap_ratio >= 0.2:
            score = 16.0
        else:
            score = 13.0

        return score, matched_domains

    def _score_location_fit(self, job: Job) -> float:
        """Score location fit (0-10 points)."""
        # Perfect remote match
        if job.location.remote and self.preferences.include_remote:
            return 10.0

        targets_lower = [t.lower() for t in self.preferences.locations]
        sf_targeted = any("san francisco" in t or "bay area" in t for t in targets_lower)
        nyc_targeted = any(
            "new york" in t or t.strip() in {"nyc", "ny"} or "manhattan" in t
            for t in targets_lower
        )

        # Check location match against any targeted region
        if job.location.is_sf_bay_area():
            return 10.0 if sf_targeted else 8.0

        if job.location.is_nyc():
            return 10.0 if nyc_targeted else 8.0

        # Hybrid with good location (rare path — kept for parity)
        if job.location.hybrid and job.location.is_sf_bay_area():
            return 9.0
        if job.location.hybrid and job.location.is_nyc():
            return 9.0

        # Remote as fallback
        if job.location.remote:
            return 7.0

        # Location doesn't match
        return 3.0

    def _score_company_preference(self, job: Job) -> float:
        """Score company preference boost (0-5 points)."""
        score = 0.0

        if job.company_type == CompanyType.AI_TOP_20:
            if self.preferences.boost_ai_companies:
                score += 3.0

        if job.company_type == CompanyType.VC_BACKED:
            if self.preferences.boost_vc_backed:
                score += 2.0

        # Both gets full 5
        if job.company_type == CompanyType.BOTH:
            score = 5.0

        return min(5.0, score)

    def _score_learned_preference(self, job: Job) -> tuple[float, list[str]]:
        """Apply learned preferences from user feedback (bonus/penalty of up to 10 points)."""
        adjustment = 0.0
        reasons = []

        # Company-level preference
        company_prefs = self.learned_prefs.get("company", {})
        if job.company in company_prefs:
            pref = company_prefs[job.company]
            weight = pref["weight"]
            count = pref["count"]
            # Scale adjustment based on sample count (more samples = more confident)
            confidence = min(1.0, count / 3)  # Full confidence at 3+ samples
            company_adj = weight * 5.0 * confidence
            adjustment += company_adj
            if weight > 0:
                reasons.append(f"You liked {job.company} jobs previously")
            elif weight < 0:
                reasons.append(f"You disliked {job.company} jobs previously")

        # Title keyword preferences
        keyword_prefs = self.learned_prefs.get("title_keyword", {})
        title_lower = job.title.lower()

        for keyword, pref in keyword_prefs.items():
            weight = pref["weight"]
            count = pref["count"]

            # Check if keyword applies to this job
            key_type, key_value = keyword.split(":", 1) if ":" in keyword else ("", keyword)

            matches = False
            if key_type == "domain":
                if key_value in title_lower:
                    matches = True
            elif key_type == "seniority":
                job_seniority = detect_job_seniority(job.title)
                if job_seniority:
                    if key_value == "senior" and job_seniority in ["Senior PM", "Staff PM", "Principal PM"]:
                        matches = True
                    elif key_value == "staff" and job_seniority in ["Staff PM", "Principal PM"]:
                        matches = True
                    elif key_value == "lead" and job_seniority in ["Group PM", "Director", "VP"]:
                        matches = True
                    elif key_value == "mid" and job_seniority in ["PM", "APM"]:
                        matches = True

            if matches:
                confidence = min(1.0, count / 3)
                keyword_adj = weight * 3.0 * confidence
                adjustment += keyword_adj
                if weight > 0:
                    reasons.append(f"Matches preferred {key_type}: {key_value}")

        # Clamp to +/- 10 points
        adjustment = max(-10.0, min(10.0, adjustment))

        return adjustment, reasons

    def _score_recency(self, job: Job) -> tuple[float, str]:
        """Compute a recency multiplier (0.5–1.0) based on posted_date."""
        if job.posted_date is None:
            return 1.0, ""

        posted = job.posted_date
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = max(0.0, (now - posted).total_seconds() / 86400.0)

        if age_days <= 7:
            return 1.0, f"Posted {age_days:.0f}d ago"
        if age_days <= 30:
            return 0.9, f"Posted {age_days:.0f}d ago"
        if age_days <= 90:
            return 0.7, f"Posted {age_days:.0f}d ago (stale)"
        return 0.5, f"Posted {age_days:.0f}d ago (very stale)"

    def score_job(self, job: Job) -> ScoredJob:
        """Calculate full score for a job."""
        # Title/seniority
        title_score, title_detail = self._score_title_seniority(job)

        # Skills overlap
        skills_score, skills_matched = self._score_skills_overlap(job)

        # Domain overlap
        domain_score, domains_matched = self._score_domain_overlap(job)

        # Location fit
        location_score = self._score_location_fit(job)

        # Company preference
        company_score = self._score_company_preference(job)

        # Learned preferences from user feedback
        learned_adjustment, learned_reasons = self._score_learned_preference(job)

        # Recency multiplier — stale postings are usually dead
        recency_multiplier, recency_detail = self._score_recency(job)

        breakdown = ScoreBreakdown(
            title_seniority=title_score,
            skills_overlap=skills_score,
            domain_overlap=domain_score,
            location_fit=location_score,
            company_preference=company_score,
            learned_adjustment=learned_adjustment,
            recency_multiplier=recency_multiplier,
            title_match_details=title_detail,
            skills_matched=skills_matched,
            domains_matched=domains_matched,
            recency_detail=recency_detail,
        )

        # Generate match reasons
        match_reasons = []

        if title_score >= 22:
            match_reasons.append(f"Strong seniority fit: {title_detail}")
        elif title_score >= 18:
            match_reasons.append(f"Good seniority fit: {title_detail}")

        if skills_matched:
            top_skills = skills_matched[:5]
            match_reasons.append(f"Matching skills: {', '.join(top_skills)}")

        if domains_matched:
            match_reasons.append(f"Domain alignment: {', '.join(domains_matched)}")

        if location_score >= 9:
            loc_desc = "Remote" if job.location.remote else job.location.format()
            match_reasons.append(f"Great location fit: {loc_desc}")

        if company_score >= 3:
            if job.company_type == CompanyType.AI_TOP_20:
                match_reasons.append("Top AI company")
            elif job.company_type == CompanyType.VC_BACKED:
                match_reasons.append("VC-backed startup")

        # Add learned preference reasons
        match_reasons.extend(learned_reasons)

        return ScoredJob(
            job=job,
            score=breakdown.total,
            score_breakdown=breakdown,
            match_reasons=match_reasons,
        )


_EXPERIENCE_RE = re.compile(
    r'\b(\d+)\+?\s*(?:to\s*\d+\s*)?years?\b',
    re.IGNORECASE,
)
_MAX_EXPERIENCE_YEARS = 6


def exceeds_experience_requirement(job: Job, max_years: int = _MAX_EXPERIENCE_YEARS) -> bool:
    """Return True if the job description explicitly requires more than max_years of experience."""
    text = job.description or ""
    for match in _EXPERIENCE_RE.finditer(text):
        years = int(match.group(1))
        if years > max_years:
            return True
    return False


def filter_jobs(
    jobs: list[Job],
    preferences: Preferences,
    posted_within_days: Optional[int] = None,
) -> list[Job]:
    """Filter jobs based on preferences.

    If `posted_within_days` is set, drops jobs older than that. Jobs with no
    posted_date are kept (we can't tell either way).
    """
    filtered = []

    include_rules = getattr(preferences, "include_rules", None) or None
    exclude_rules = getattr(preferences, "exclude_rules", None) or None

    cutoff = None
    if posted_within_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)

    for job in jobs:
        if cutoff is not None and job.posted_date is not None:
            posted = job.posted_date
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if posted < cutoff:
                continue

        # Check title
        if not is_pm_title(job.title, preferences.allowed_titles, include_rules=include_rules):
            continue

        # Check exclusions
        if should_exclude_job(job, preferences.excluded_keywords, exclude_rules=exclude_rules):
            continue

        # Check required keywords
        if not has_required_keywords(job, preferences.required_keywords):
            continue

        # Check location
        if not matches_location(
            job,
            preferences.location,
            preferences.include_remote,
        ):
            continue

        # Skip roles requiring more than 6 years of experience
        if exceeds_experience_requirement(job):
            continue

        filtered.append(job)

    logger.info(
        "jobs_filtered",
        input_count=len(jobs),
        output_count=len(filtered),
    )

    return filtered


def score_and_rank_jobs(
    jobs: list[Job],
    cv_signals: CVSignals,
    preferences: Preferences,
    max_results: int = 15,
) -> list[ScoredJob]:
    """Score and rank jobs, returning top N."""
    scorer = JobScorer(cv_signals, preferences)

    scored_jobs = [scorer.score_job(job) for job in jobs]

    # Sort by score descending
    scored_jobs.sort(key=lambda x: -x.score)

    # Assign ranks and limit
    for i, sj in enumerate(scored_jobs[:max_results], 1):
        sj.rank = i

    logger.info(
        "jobs_scored_and_ranked",
        total_scored=len(scored_jobs),
        returned=min(max_results, len(scored_jobs)),
        top_score=scored_jobs[0].score if scored_jobs else 0,
    )

    return scored_jobs[:max_results]
