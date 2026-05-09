"""LLM-powered company review generator for role-radar.

For a target company, generates an investor-grade analysis document covering
valuation, funding history, growth trajectory, product/market position,
team, press/media sentiment, employee sentiment (Glassdoor/Levels.fyi), and
community signal (Reddit, HN). Uses Claude with the server-side web search
tool to pull current data.

Public entry point: `generate_review_for_company()`.
"""

from role_radar.company_review.generator import (
    CompanyReviewGenerationError,
    CompanyReviewGenerator,
    generate_review_for_company,
)
from role_radar.company_review.models import CompanyReviewDoc

__all__ = [
    "CompanyReviewDoc",
    "CompanyReviewGenerationError",
    "CompanyReviewGenerator",
    "generate_review_for_company",
]
