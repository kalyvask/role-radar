"""CV/Resume parsing module."""

import re
from pathlib import Path
from typing import Optional

from role_radar.models import CVSignals
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


def extract_text_from_pdf(path: Path) -> str:
    """Extract text from a PDF file."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        text_parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
        return "\n\n".join(text_parts)
    except Exception as e:
        logger.error("pdf_extraction_failed", path=str(path), error=str(e))
        raise


def extract_text_from_docx(path: Path) -> str:
    """Extract text from a DOCX file."""
    try:
        from docx import Document

        doc = Document(path)
        text_parts = []
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)
        return "\n\n".join(text_parts)
    except Exception as e:
        logger.error("docx_extraction_failed", path=str(path), error=str(e))
        raise


def extract_text_from_txt(path: Path) -> str:
    """Extract text from a plain text file."""
    return path.read_text(encoding="utf-8")


def extract_text(path: Path) -> str:
    """Extract text from CV file (PDF, DOCX, or TXT)."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    elif suffix == ".docx":
        return extract_text_from_docx(path)
    elif suffix in (".txt", ".md", ".rst"):
        return extract_text_from_txt(path)
    else:
        raise ValueError(f"Unsupported file format: {suffix}")


# Known skills keywords
PM_SKILLS = {
    # Technical
    "sql", "python", "data analysis", "analytics", "a/b testing",
    "ab testing", "api", "apis", "machine learning", "ml", "ai",
    "artificial intelligence", "deep learning", "nlp", "natural language",
    "computer vision", "llm", "large language model", "gpt", "transformers",
    "product analytics", "amplitude", "mixpanel", "segment", "tableau",
    "looker", "data science", "statistics", "experimentation",

    # Product
    "product management", "product strategy", "roadmap", "roadmapping",
    "user research", "customer discovery", "market research", "competitive analysis",
    "product development", "agile", "scrum", "kanban", "jira", "confluence",
    "sprint planning", "backlog", "prioritization", "prd", "product requirements",
    "user stories", "okrs", "kpis", "metrics", "go-to-market", "gtm",
    "product-market fit", "pmf", "mvp", "prototype", "figma", "design thinking",

    # Domain-specific
    "b2b", "b2c", "saas", "enterprise", "platform", "infrastructure",
    "developer tools", "devtools", "cloud", "aws", "gcp", "azure",
    "mobile", "ios", "android", "web", "frontend", "backend",
    "payments", "fintech", "healthcare", "e-commerce", "marketplace",
    "ads", "advertising", "growth", "retention", "engagement",

    # Leadership
    "leadership", "team management", "cross-functional", "stakeholder management",
    "executive communication", "strategy", "vision", "mentoring",
}

# Domain keywords
DOMAINS = {
    "ai/ml": ["ai", "ml", "machine learning", "artificial intelligence", "deep learning",
              "nlp", "computer vision", "llm", "gpt", "transformers", "neural network"],
    "infrastructure": ["infrastructure", "infra", "platform", "cloud", "aws", "gcp",
                       "azure", "kubernetes", "docker", "devops", "sre", "reliability"],
    "developer tools": ["developer tools", "devtools", "sdk", "api", "developer experience",
                        "dx", "developer platform", "ide", "cli"],
    "b2b saas": ["b2b", "saas", "enterprise", "business software", "crm", "erp"],
    "fintech": ["fintech", "financial", "payments", "banking", "crypto", "blockchain"],
    "consumer": ["consumer", "b2c", "social", "mobile app", "gaming", "entertainment"],
    "healthcare": ["healthcare", "health tech", "medical", "clinical", "biotech"],
    "e-commerce": ["e-commerce", "ecommerce", "marketplace", "retail", "shopping"],
    "security": ["security", "cybersecurity", "infosec", "privacy", "compliance"],
    "data": ["data", "analytics", "data platform", "data warehouse", "etl", "bi"],
}

# Title patterns for seniority detection
SENIORITY_PATTERNS = {
    "VP": [r"\bvp\b", r"vice president", r"vp of product", r"vp,? product"],
    "Director": [r"\bdirector\b", r"head of product", r"director of product"],
    "Group PM": [r"group pm", r"group product", r"gpm"],
    "Principal PM": [r"principal pm", r"principal product", r"staff product", r"staff pm"],
    "Senior PM": [r"senior pm", r"senior product", r"sr\.? pm", r"sr\.? product"],
    "PM": [r"\bpm\b", r"product manager", r"product management"],
    "APM": [r"\bapm\b", r"associate product", r"associate pm"],
}


def detect_seniority(titles: list[str]) -> Optional[str]:
    """Detect seniority level from job titles."""
    # Priority order (highest first). APM must come before the generic "PM"
    # rule: "Associate Product Manager" contains "product manager", so a PM-first
    # order would label it "PM" before the more specific "associate product" hit.
    seniority_order = ["VP", "Director", "Group PM", "Principal PM", "Senior PM", "APM", "PM"]

    combined_titles = " ".join(titles).lower()

    for level in seniority_order:
        patterns = SENIORITY_PATTERNS[level]
        for pattern in patterns:
            if re.search(pattern, combined_titles, re.IGNORECASE):
                return level

    return None


def extract_titles(text: str) -> tuple[list[str], list[str]]:
    """Extract job titles from CV text.

    Returns (recent_titles, all_titles).
    """
    title_patterns = [
        r"(?:^|\n)\s*((?:Senior|Sr\.?|Lead|Principal|Staff|Associate|Group|Head of|VP|Director|Manager|Technical|AI|Platform|Growth)?\s*(?:Product Manager|PM|Technical PM|AI PM|Platform PM|Growth PM)[^,\n]*)",
        r"(?:title|position|role)[\s:]+([^\n,]+(?:product|pm)[^\n,]*)",
    ]

    titles = []
    for pattern in title_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        titles.extend([m.strip() for m in matches if len(m.strip()) < 100])

    # Dedupe while preserving order
    seen = set()
    unique_titles = []
    for t in titles:
        t_lower = t.lower()
        if t_lower not in seen:
            seen.add(t_lower)
            unique_titles.append(t)

    # First few titles are likely recent
    recent = unique_titles[:3] if unique_titles else []

    return recent, unique_titles


def _skill_pattern(skill: str) -> re.Pattern:
    """Build a word-boundary-aware regex for a skill, including multi-word skills.

    `re.escape` preserves slashes/spaces; `\\b` correctly anchors on word characters
    at the start/end of the skill, which avoids false positives like matching
    "a/b testing" inside "available by Tuesday testing".
    """
    return re.compile(rf"(?<!\w){re.escape(skill)}(?!\w)", re.IGNORECASE)


# Cache compiled patterns at import time — extract_skills runs once per CV but
# multiple times in tests, and pattern compilation is the dominant cost.
_SKILL_PATTERNS = {skill: _skill_pattern(skill) for skill in PM_SKILLS}
_DOMAIN_PATTERNS = {
    domain: [_skill_pattern(kw) for kw in keywords]
    for domain, keywords in DOMAINS.items()
}


def extract_skills(text: str) -> list[str]:
    """Extract skills from CV text using word-boundary matching."""
    found_skills = [skill for skill, pat in _SKILL_PATTERNS.items() if pat.search(text)]
    return sorted(set(found_skills))


def extract_domains(text: str) -> list[str]:
    """Extract domain expertise from CV text using word-boundary matching."""
    found_domains = []
    for domain, patterns in _DOMAIN_PATTERNS.items():
        if any(pat.search(text) for pat in patterns):
            found_domains.append(domain)
    return sorted(set(found_domains))


def extract_companies(text: str) -> list[str]:
    """Extract company names from CV text."""
    # Look for common patterns like "at Company" or "Company - Title"
    company_patterns = [
        r"(?:at|@)\s+([A-Z][A-Za-z0-9\s&]+?)(?:\s*[-,]|\s+as\b|\s+from\b)",
        r"([A-Z][A-Za-z0-9\s&]+?)\s*[-|]\s*(?:Senior|Sr\.?|Lead|Principal|Staff|Associate|Group|Head|VP|Director|Manager|Product)",
    ]

    companies = []
    for pattern in company_patterns:
        matches = re.findall(pattern, text)
        companies.extend([m.strip() for m in matches if 2 < len(m.strip()) < 50])

    # Filter out common false positives
    false_positives = {"senior", "lead", "product", "manager", "the", "and", "or"}
    companies = [c for c in companies if c.lower() not in false_positives]

    return list(set(companies))[:10]


def extract_years_experience(text: str) -> Optional[int]:
    """Estimate years of experience from CV text."""
    # Look for explicit mentions. The first pattern allows a few words between
    # "years of" and "experience" so "8+ years of product management experience"
    # still resolves to 8 (not just the bare "8 years of experience" phrasing).
    patterns = [
        r"(\d+)\+?\s*years?\s+(?:of\s+)?(?:[\w&/.-]+\s+){0,5}?(?:experience|exp)\b",
        r"(?:experience|exp)[\s:]+(\d+)\+?\s*years?",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    # Try to count from work history
    year_pattern = r"\b(20[0-2]\d|19\d{2})\b"
    years = re.findall(year_pattern, text)
    if years:
        years = sorted([int(y) for y in years])
        if years:
            from datetime import datetime
            current_year = datetime.now().year
            return current_year - min(years)

    return None


def parse_cv(path: Path) -> CVSignals:
    """Parse CV and extract structured signals."""
    logger.info("parsing_cv", path=str(path))

    # Extract text
    raw_text = extract_text(path)
    logger.debug("cv_text_extracted", chars=len(raw_text))

    # Extract signals
    recent_titles, all_titles = extract_titles(raw_text)
    skills = extract_skills(raw_text)
    domains = extract_domains(raw_text)
    companies = extract_companies(raw_text)
    years_exp = extract_years_experience(raw_text)
    seniority = detect_seniority(all_titles) or detect_seniority(recent_titles)

    # Extract general keywords (words that appear frequently)
    words = re.findall(r"\b[a-z]{4,}\b", raw_text.lower())
    word_freq = {}
    for w in words:
        word_freq[w] = word_freq.get(w, 0) + 1
    keywords = [w for w, c in sorted(word_freq.items(), key=lambda x: -x[1])[:50]]

    signals = CVSignals(
        raw_text=raw_text,
        recent_titles=recent_titles,
        all_titles=all_titles,
        skills=skills,
        domains=domains,
        keywords=keywords,
        companies=companies,
        years_experience=years_exp,
        inferred_seniority=seniority,
    )

    logger.info(
        "cv_parsed",
        titles_found=len(all_titles),
        skills_found=len(skills),
        domains_found=len(domains),
        inferred_seniority=seniority,
        years_experience=years_exp,
    )

    return signals
