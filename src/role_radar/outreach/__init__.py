"""Cold outreach drafting for jobs in the agent pipeline.

Sibling to `interview_prep/`. Generates a short, personalized cold email
the candidate can send to a recruiter or hiring manager for a specific job.
"""

from role_radar.outreach.generator import (
    OutreachGenerationError,
    OutreachGenerator,
    generate_outreach_for_job,
)
from role_radar.outreach.models import OutreachDraft

__all__ = [
    "OutreachDraft",
    "OutreachGenerator",
    "OutreachGenerationError",
    "generate_outreach_for_job",
]
