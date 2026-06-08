"""Company- and VC-name normalization for matching connections to companies.

Matching is deliberately conservative: two names match only when their
normalized forms are *equal*. A wrong warm intro ("you know someone at X" when
you don't) is embarrassing and erodes trust in the whole feature, so we favor
false negatives over false positives. Normalization strips legal suffixes and
applies a small, high-confidence alias map; it does NOT do fuzzy/substring
matching.
"""

from __future__ import annotations

import re

# Legal-entity suffixes dropped before comparison ("OpenAI, Inc." -> "openai").
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "sa", "srl", "bv", "oy", "ab",
    "pte", "llp", "lp", "pbc", "holdings", "group", "labs", "technologies",
}

# Tokens that are noise inside company names.
_STOPWORDS = {"the"}

# Canonical aliases applied AFTER basic normalization (normalized -> normalized).
# Keep small and high-confidence; each entry is a name people actually use.
_COMPANY_ALIASES = {
    "facebook": "meta",
    "meta ai": "meta",
    "google deepmind": "deepmind",
    "google brain": "google",
    "x corp": "x",
    "twitter": "x",
}

# Firm-type suffix tokens dropped from VC names so an investor recorded as
# "Sequoia" matches a connection whose employer is "Sequoia Capital".
_VC_SUFFIXES = {
    "capital", "ventures", "venture", "partners", "partner", "management",
    "fund", "funds", "group", "holdings", "associates",
}

# VC-firm aliases for Tier-2 (investor) matching. Investors get their own map
# because the common short forms differ from operating-company conventions.
# Keys are matched against the name BEFORE generic suffix-stripping, so an
# alias like "google ventures" wins before it would collapse to "google".
_VC_ALIASES = {
    "a16z": "andreessen horowitz",
    "andreessen horowitz a16z": "andreessen horowitz",
    "kpcb": "kleiner perkins",
    "kleiner perkins caufield byers": "kleiner perkins",
    "google ventures": "gv",
    "gv google ventures": "gv",
    "yc": "y combinator",
    "ycombinator": "y combinator",
    "nea": "new enterprise associates",
}

# Mega-cap strategic investors. These back many startups directly, but they
# also employ tens of thousands of people, so "you know someone there" is not a
# meaningful warm path *into the portfolio company*. We exclude them from Tier-2
# bridging (a genuine colleague at the company is still surfaced as Tier 1).
# Stored as normalize_vc() forms. Edit freely — one line per firm.
STRATEGIC_INVESTORS = {
    "amazon", "aws", "google", "alphabet", "microsoft", "meta", "apple",
    "nvidia", "salesforce", "oracle", "sap", "cisco", "ibm", "intel",
    "qualcomm", "samsung", "adobe", "databricks", "snowflake", "uber",
    "paypal", "tencent", "alibaba", "baidu", "sony", "dell", "broadcom",
    "servicenow", "workday", "comcast", "disney", "bytedance",
}

_PUNCT_RE = re.compile(r"[^\w\s&]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _base_normalize(name: str) -> str:
    """Lowercase, strip punctuation, drop legal suffixes and stopwords."""
    if not name:
        return ""
    text = name.lower().strip()
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    tokens = [
        t for t in text.split(" ")
        if t and t not in _LEGAL_SUFFIXES and t not in _STOPWORDS
    ]
    return " ".join(tokens)


def normalize_company(name: str) -> str:
    """Normalize an operating-company name for Tier-1 matching."""
    base = _base_normalize(name)
    return _COMPANY_ALIASES.get(base, base)


def normalize_vc(name: str) -> str:
    """Normalize a VC / investor firm name for Tier-2 matching.

    Drops firm-type suffixes (Capital, Ventures, Partners, ...) so short and
    long forms of the same fund unify. Aliases are checked first, on the
    pre-strip form, so e.g. "Google Ventures" maps to "gv" instead of
    collapsing to "google".
    """
    base = _base_normalize(name)
    if base in _VC_ALIASES:
        return _VC_ALIASES[base]
    tokens = [t for t in base.split(" ") if t and t not in _VC_SUFFIXES]
    stripped = " ".join(tokens) if tokens else base
    return _VC_ALIASES.get(stripped, stripped)


def is_strategic_investor(name: str) -> bool:
    """True if `name` is a mega-cap strategic investor excluded from Tier 2.

    Note: a corporate VC arm like "Google Ventures" normalizes to "gv" (a real
    fund worth bridging), so only the parent "Google" is excluded.
    """
    return normalize_vc(name) in STRATEGIC_INVESTORS
