"""Structured output schema for the interview prep document.

Each leaf string is Markdown — the LLM fills in formatted prose, the renderers
just glue sections together with headers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RoleHeader(BaseModel):
    """Header block of the prep doc."""

    model_config = ConfigDict(extra="forbid")

    company: str
    role_title: str
    location: str
    compensation: Optional[str] = Field(
        default=None, description="e.g. '$160K-$230K base + bonus + equity', or null if unknown"
    )
    candidate_name: str


class TopicToKnow(BaseModel):
    """One technical/strategic topic the candidate must know cold."""

    model_config = ConfigDict(extra="forbid")

    section_number: str = Field(description="e.g. '2.1', '2.2'")
    title: str
    body_markdown: str = Field(
        description=(
            "Markdown body. Include 'Key facts' and 'Why this matters for the interview' "
            "as bold sub-headers when relevant."
        )
    )


class InterviewQuestion(BaseModel):
    """One likely interview question with a sample answer."""

    model_config = ConfigDict(extra="forbid")

    section_number: str = Field(description="e.g. '4.1', '4.2'")
    category: str = Field(
        description=(
            "Category label, e.g. 'Why this team', 'Technical depth', 'Product sense', "
            "'Strategy', 'Behavioral', 'Trade-off', 'Customer empathy'"
        )
    )
    question: str
    approach: str = Field(description="2-4 sentences on how to structure the answer.")
    sample_answer_markdown: str = Field(
        description="The actual answer the candidate could give, in their voice."
    )
    why_this_works: str = Field(description="2-3 sentences on what makes this answer strong.")


class BackgroundFit(BaseModel):
    """How the candidate's background maps to the role."""

    model_config = ConfigDict(extra="forbid")

    what_you_bring: list[str] = Field(
        description="3-6 bullets, each a specific strength tied to the candidate's CV."
    )
    honest_gaps: list[str] = Field(
        description="2-4 bullets, each a real gap stated directly. Do not whitewash."
    )
    how_to_frame_gap_markdown: str = Field(
        description=(
            "The exact framing the candidate should use in the room — a 3-5 sentence "
            "verbatim quote-style answer that names the gap and pivots to strength."
        )
    )


class PrepDay(BaseModel):
    """One day (or block) of the multi-day prep plan."""

    model_config = ConfigDict(extra="forbid")

    day: str = Field(description="e.g. 'Day 1-2', 'Day 3'")
    focus: str = Field(description="Short label, e.g. 'Foundational reading'")
    items_markdown: str = Field(description="Markdown bullet list of concrete to-dos.")


class Closing(BaseModel):
    """Final section: questions to ask, disqualifiers, strongest single move."""

    model_config = ConfigDict(extra="forbid")

    questions_to_ask: list[str] = Field(
        description="3 questions to ask the interviewer, ranked by signal."
    )
    disqualifiers: list[str] = Field(
        description="3-5 specific things that will tank this interview."
    )
    strongest_single_move_markdown: str = Field(
        description=(
            "The single most important thing the candidate should do in the interview. "
            "Be opinionated and specific — not generic advice."
        )
    )
    final_note_markdown: Optional[str] = Field(
        default=None,
        description="Optional closing reality check on whether this role is a stretch / a fit / a layup.",
    )


class PrepDoc(BaseModel):
    """The full structured interview prep document."""

    model_config = ConfigDict(extra="forbid")

    header: RoleHeader
    role_summary_markdown: str = Field(
        description=(
            "Section 1: 'What this role actually is' — 2-4 paragraph plain-English read of "
            "the JD, the product surface broken down as a Markdown bullet list, and "
            "(if applicable) how this role differs from adjacent roles."
        )
    )
    topics_to_know: list[TopicToKnow] = Field(
        description=(
            "Section 2: 'Topics you must know cold'. 3-6 topics, each substantive enough "
            "to discuss with the hiring manager. Pull from the company's public engineering "
            "blog, docs, and product surface."
        )
    )
    strategic_angle_markdown: Optional[str] = Field(
        default=None,
        description=(
            "Section 3: Optional. If the JD has a clear strategic emphasis (AI, infra, "
            "platform shift), 2-4 paragraphs developing the candidate's view on it. "
            "Otherwise null."
        ),
    )
    likely_questions: list[InterviewQuestion] = Field(
        description=(
            "Section 4: 5-7 interview questions across categories (why-this-team, technical "
            "depth, product sense, strategy, behavioral, trade-off, customer empathy)."
        )
    )
    background_fit: BackgroundFit = Field(
        description="Section 5: How the candidate's background maps + gaps + framing."
    )
    prep_plan: list[PrepDay] = Field(
        description="Section 6: 5-7 day prep plan with concrete reading and rehearsal tasks."
    )
    closing: Closing = Field(
        description="Section 7: Questions to ask, disqualifiers, the strongest single move."
    )
    sources_markdown: Optional[str] = Field(
        default=None,
        description=(
            "Optional Markdown bullet list of sources to read (engineering blog posts, docs, "
            "earnings calls, key people on LinkedIn)."
        ),
    )

    # Metadata (filled in by the generator, not the LLM)
    generated_at: Optional[datetime] = Field(default=None, exclude=True)
