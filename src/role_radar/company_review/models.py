"""Schema for the company review document.

Unlike the interview prep doc (which uses Claude's structured-output API),
the company review is generated as Markdown directly by Claude using the
server-side web_search tool — structured output and tool use don't compose
cleanly. This module defines a thin wrapper carrying the generated Markdown
plus metadata pulled from the model's response (sources, token usage,
search queries used).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CompanyReviewSource(BaseModel):
    """One web source the model cited during research."""

    url: str
    title: Optional[str] = None
    # Workflow tag so the renderer can group by section in the Sources block.
    section: Optional[str] = None


class CompanyReviewDoc(BaseModel):
    """The generated company review."""

    company: str
    markdown: str = Field(
        description="The full review document as Markdown, ready to render to HTML/DOCX."
    )

    # Recommendation metadata extracted from the model's output (best-effort).
    overall_signal: Optional[str] = Field(
        default=None,
        description="One of: 'Strong apply', 'Apply with caveats', 'Pass', 'Inconclusive'.",
    )
    headline_summary: Optional[str] = Field(
        default=None,
        description="One-paragraph TL;DR pulled from the top of the doc.",
    )

    # Tracing
    sources: list[CompanyReviewSource] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)

    # Token + timing telemetry
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    web_search_count: Optional[int] = None
    duration_seconds: Optional[float] = None
    model: Optional[str] = None
    generated_at: Optional[datetime] = None
