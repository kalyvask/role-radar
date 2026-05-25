"""Data models for Role Radar."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class CompanyType(str, Enum):
    """Type of company."""
    AI_TOP_20 = "ai_top_20"
    VC_BACKED = "vc_backed"
    BOTH = "both"


class ATSType(str, Enum):
    """Applicant Tracking System type."""
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    WORKDAY = "workday"
    BAMBOOHR = "bamboohr"
    GENERIC_HTML = "generic_html"
    UNKNOWN = "unknown"


class AICategory(str, Enum):
    """AI company category for scoring."""
    FRONTIER_LAB = "frontier_lab"
    AI_INFRA = "ai_infra"
    AI_APPS = "ai_apps"
    AI_ADJACENT = "ai_adjacent"
    AI_DEV_TOOLS = "ai_dev_tools"


class Company(BaseModel):
    """Company model with all metadata."""
    name: str
    slug: str = ""
    company_type: CompanyType
    homepage: Optional[str] = None
    careers_url: Optional[str] = None
    ats_type: ATSType = ATSType.UNKNOWN
    ats_identifier: Optional[str] = None  # e.g., Greenhouse board token
    scraping_allowed: bool = True
    notes: Optional[str] = None

    # AI company specific
    ai_category: Optional[AICategory] = None
    ai_score: Optional[float] = None

    # VC-backed specific
    backed_by: list[str] = Field(default_factory=list)
    funding_stage: Optional[str] = None

    def model_post_init(self, __context) -> None:
        if not self.slug:
            self.slug = self.name.lower().replace(" ", "-").replace(".", "")


class JobLocation(BaseModel):
    """Job location details."""
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    remote: bool = False
    hybrid: bool = False
    raw_location: str = ""

    def is_sf_bay_area(self) -> bool:
        """Check if location is in SF Bay Area."""
        sf_keywords = [
            "san francisco", "sf", "bay area", "palo alto", "mountain view",
            "menlo park", "sunnyvale", "cupertino", "santa clara", "san jose",
            "redwood city", "oakland", "berkeley", "fremont", "san mateo",
            "south bay", "east bay", "peninsula", "silicon valley"
        ]
        location_lower = self.raw_location.lower()
        return any(kw in location_lower for kw in sf_keywords)

    def is_nyc(self) -> bool:
        """Check if location is in the NYC metro area.

        Substring matching is intentionally conservative: short tokens like
        "NY" or "NYC" alone would false-positive on "Albany, NY" / "Anyc...",
        so we require either a distinctive phrase, an exact city match, or a
        word-boundary "NYC" token.
        """
        location_lower = self.raw_location.lower()

        nyc_phrases = [
            "new york",
            "manhattan", "brooklyn", "queens, ny", "the bronx",
            "harlem", "soho", "tribeca", "midtown",
            "long island city", "jersey city", "hoboken", "newark, nj",
        ]
        if any(kw in location_lower for kw in nyc_phrases):
            return True

        # Word-boundary check for the NYC abbreviation so "Anyc..." won't match.
        import re
        if re.search(r"\bnyc\b", location_lower):
            return True

        # Explicit city + state combo
        if self.state and self.state.strip().upper().replace(".", "") == "NY":
            city = (self.city or "").lower()
            nyc_cities = {
                "new york", "new york city", "brooklyn", "queens",
                "bronx", "manhattan",
            }
            if city in nyc_cities:
                return True
        return False

    def format(self) -> str:
        """Format location for display."""
        parts = []
        if self.city:
            parts.append(self.city)
        if self.state:
            parts.append(self.state)

        modifiers = []
        if self.remote:
            modifiers.append("Remote")
        if self.hybrid:
            modifiers.append("Hybrid")

        location = ", ".join(parts) if parts else self.raw_location
        if modifiers:
            location += f" ({'/'.join(modifiers)})"
        return location or "Location not specified"


class SalaryInfo(BaseModel):
    """Salary information for a job."""
    min_salary: Optional[int] = None
    max_salary: Optional[int] = None
    currency: str = "USD"
    interval: str = "year"  # year, month, hour
    is_estimated: bool = False  # True if salary is estimated, False if from posting

    def format(self) -> str:
        """Format salary for display."""
        if not self.min_salary and not self.max_salary:
            return "Not specified"

        prefix = "~" if self.is_estimated else ""

        if self.min_salary and self.max_salary:
            if self.min_salary == self.max_salary:
                return f"{prefix}${self.min_salary:,}/yr"
            return f"{prefix}${self.min_salary:,} - ${self.max_salary:,}/yr"
        elif self.min_salary:
            return f"{prefix}${self.min_salary:,}+/yr"
        else:
            return f"{prefix}Up to ${self.max_salary:,}/yr"


class Job(BaseModel):
    """Normalized job posting model."""
    id: str  # Unique identifier (company_slug + external_id)
    external_id: str  # ID from the ATS
    company: str
    company_slug: str
    company_type: CompanyType
    title: str
    location: JobLocation
    description: Optional[str] = None
    apply_url: str
    posted_date: Optional[datetime] = None
    department: Optional[str] = None
    employment_type: Optional[str] = None  # full-time, contract, etc.
    seniority: Optional[str] = None  # APM, PM, Sr PM, etc.
    salary: Optional[SalaryInfo] = None  # Salary information

    # Source tracking
    source_ats: ATSType = ATSType.UNKNOWN
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    raw_data: Optional[dict] = None

    def model_post_init(self, __context) -> None:
        if not self.id:
            self.id = f"{self.company_slug}_{self.external_id}"


class CVSignals(BaseModel):
    """Structured signals extracted from CV."""
    raw_text: str

    # Extracted signals
    recent_titles: list[str] = Field(default_factory=list)
    all_titles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    years_experience: Optional[int] = None

    # Inferred seniority
    inferred_seniority: Optional[str] = None  # APM, PM, Sr PM, Group PM, Director, VP


class ScoreBreakdown(BaseModel):
    """Detailed score breakdown for a job match."""
    title_seniority: float = 0.0  # 0-25
    skills_overlap: float = 0.0   # 0-35
    domain_overlap: float = 0.0   # 0-25
    location_fit: float = 0.0     # 0-10
    company_preference: float = 0.0  # 0-5
    learned_adjustment: float = 0.0  # -10 to +10 (from user feedback)
    recency_multiplier: float = 1.0  # 0.5-1.0 (stale roles ranked lower)

    title_match_details: str = ""
    skills_matched: list[str] = Field(default_factory=list)
    domains_matched: list[str] = Field(default_factory=list)
    recency_detail: str = ""

    @property
    def total(self) -> float:
        base_score = (
            self.title_seniority +
            self.skills_overlap +
            self.domain_overlap +
            self.location_fit +
            self.company_preference
        )
        adjusted = (base_score + self.learned_adjustment) * self.recency_multiplier
        return max(0.0, min(100.0, adjusted))

    def to_dict(self) -> dict:
        result = {
            "title_seniority": f"{self.title_seniority:.1f}/25",
            "skills_overlap": f"{self.skills_overlap:.1f}/35",
            "domain_overlap": f"{self.domain_overlap:.1f}/25",
            "location_fit": f"{self.location_fit:.1f}/10",
            "company_preference": f"{self.company_preference:.1f}/5",
            "total": f"{self.total:.1f}/100",
        }
        if self.learned_adjustment != 0:
            sign = "+" if self.learned_adjustment > 0 else ""
            result["learned_adjustment"] = f"{sign}{self.learned_adjustment:.1f}"
        if self.recency_multiplier < 1.0:
            result["recency_multiplier"] = f"x{self.recency_multiplier:.2f}"
        return result


class ScoredJob(BaseModel):
    """Job with scoring information."""
    job: Job
    score: float
    score_breakdown: ScoreBreakdown
    match_reasons: list[str] = Field(default_factory=list)
    rank: int = 0


class RunSummary(BaseModel):
    """Summary of a role-radar run."""
    run_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None

    # Counts
    companies_processed: int = 0
    companies_with_errors: int = 0
    total_jobs_found: int = 0
    jobs_after_filter: int = 0
    jobs_after_dedupe: int = 0
    jobs_scored: int = 0
    jobs_in_email: int = 0

    # Details
    companies_skipped: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    # Outputs
    email_sent: bool = False
    report_path: Optional[str] = None
