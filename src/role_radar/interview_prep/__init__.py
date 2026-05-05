"""LLM-powered interview prep generator for role-radar.

Generates a candidate-tailored, opinionated interview prep document for a given
job posting and CV. Uses Claude Opus 4.7 with prompt caching for the static
context (frameworks, calibrations, company playbooks), and emits both Markdown
and DOCX.

Public entry point: `generate_prep_for_job()`.
"""

from role_radar.interview_prep.generator import (
    InterviewPrepGenerator,
    PrepGenerationError,
    generate_prep_for_job,
)
from role_radar.interview_prep.models import PrepDoc
from role_radar.interview_prep.reviewer import (
    ReviewReport,
    render_review_markdown,
    review_prep_doc,
)

__all__ = [
    "InterviewPrepGenerator",
    "PrepDoc",
    "PrepGenerationError",
    "ReviewReport",
    "generate_prep_for_job",
    "render_review_markdown",
    "review_prep_doc",
]
