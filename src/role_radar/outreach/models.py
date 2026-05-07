"""Structured output schema for cold outreach drafts."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class OutreachDraft(BaseModel):
    """A single cold-outreach draft for one job."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(
        description=(
            "Email subject line. Concise, specific, no clickbait. "
            "Should reference the role or a concrete hook. Under 60 chars."
        )
    )
    body: str = Field(
        description=(
            "The email body. Plain text, no HTML. 100-200 words. "
            "Opens with a specific hook (not 'I hope this finds you well'). "
            "Names one concrete reason this candidate fits this role. "
            "Ends with a short, low-pressure ask. "
            "First-person, conversational. No em-dashes."
        )
    )
    rationale: str = Field(
        description=(
            "1-2 sentences explaining why this draft will work for this "
            "specific job and candidate. Not visible to the recipient."
        )
    )
    self_rating: int = Field(
        ge=1,
        le=10,
        description=(
            "Honest 1-10 rating of the draft's quality. "
            "10 = ready to send, hits the brief. "
            "5 = okay, would send under time pressure. "
            "<5 = should be regenerated. "
            "Be honest; do not inflate."
        ),
    )

    # Metadata (filled in by the generator, not the LLM)
    generated_at: Optional[datetime] = Field(default=None, exclude=True)
