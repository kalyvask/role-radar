"""Anthropic-backed generator for outreach drafts.

Mirrors `interview_prep.generator` — same model, same caching pattern, same
gotchas. The system prompt holds voice rules and the optional candidate
profile (cacheable across many jobs); the user prompt holds the per-job
context.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from role_radar.models import CVSignals, Job
from role_radar.outreach.models import OutreachDraft
from role_radar.outreach.prompts import build_system_prompt, build_user_prompt
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


MODEL_ID = "claude-opus-4-7"
MAX_TOKENS = 4000


class OutreachGenerationError(RuntimeError):
    """Raised when the generator fails to produce a valid outreach draft."""


class OutreachGenerator:
    """Generates structured outreach drafts by calling Claude.

    Holds an Anthropic client and generation defaults. Stateless across
    calls otherwise. Reuse for many jobs in the same process.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL_ID,
        max_tokens: int = MAX_TOKENS,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise OutreachGenerationError(
                "The `anthropic` package is required for outreach drafting. "
                "Install with: pip install 'role-radar[interview]' or `pip install anthropic`."
            ) from e

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif not os.getenv("ANTHROPIC_API_KEY"):
            raise OutreachGenerationError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env or pass api_key explicitly."
            )

        self._client = anthropic.Anthropic(**kwargs)
        self._anthropic_module = anthropic
        self._model = model
        self._max_tokens = max_tokens

    def generate(
        self,
        job: Job,
        cv: CVSignals,
        candidate_name: str,
        *,
        contact_name: Optional[str] = None,
        contact_role: Optional[str] = None,
        cv_excerpt: Optional[str] = None,
        voice_notes_md: Optional[str] = None,
        candidate_profile_md: Optional[str] = None,
    ) -> OutreachDraft:
        """Generate one outreach draft for a single job.

        Args:
            job: The job posting.
            cv: Parsed CV signals.
            candidate_name: Candidate name for the sign-off (first name used).
            contact_name: Optional recipient name.
            contact_role: Optional recipient role (e.g. "recruiter").
            cv_excerpt: Optional raw CV text (capped to ~3K chars).
            voice_notes_md: Optional Markdown overriding the default voice rules.
            candidate_profile_md: Optional Markdown profile of the candidate
                for richer personalization than CV alone.

        Returns:
            A validated `OutreachDraft` instance.
        """
        system_prompt = build_system_prompt(
            voice_notes_md=voice_notes_md,
            candidate_profile_md=candidate_profile_md,
        )
        user_prompt = build_user_prompt(
            job=job,
            cv=cv,
            candidate_name=candidate_name,
            contact_name=contact_name,
            contact_role=contact_role,
            cv_excerpt=cv_excerpt,
        )

        logger.info(
            "outreach_generation_started",
            company=job.company,
            title=job.title,
            model=self._model,
            system_prompt_chars=len(system_prompt),
            user_prompt_chars=len(user_prompt),
        )

        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
                output_format=OutreachDraft,
            )
        except self._anthropic_module.APIStatusError as e:
            raise OutreachGenerationError(
                f"Anthropic API error ({e.status_code}): {e.message}"
            ) from e
        except self._anthropic_module.APIConnectionError as e:
            raise OutreachGenerationError(
                f"Network error reaching Anthropic API: {e}"
            ) from e

        if response.stop_reason == "refusal":
            raise OutreachGenerationError(
                "Claude refused to generate an outreach draft for this job."
            )

        if response.stop_reason == "max_tokens":
            raise OutreachGenerationError(
                f"Hit max_tokens ({self._max_tokens}) before draft completed."
            )

        draft = response.parsed_output
        if draft is None:
            raise OutreachGenerationError(
                "Anthropic returned a response with no parsed_output."
            )

        draft.generated_at = datetime.utcnow()

        usage = response.usage
        logger.info(
            "outreach_generation_completed",
            company=job.company,
            title=job.title,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            self_rating=draft.self_rating,
        )

        return draft


def generate_outreach_for_job(
    job: Job,
    cv: CVSignals,
    candidate_name: str,
    *,
    contact_name: Optional[str] = None,
    contact_role: Optional[str] = None,
    cv_excerpt: Optional[str] = None,
    voice_notes_md: Optional[str] = None,
    candidate_profile_md: Optional[str] = None,
    api_key: Optional[str] = None,
) -> OutreachDraft:
    """One-shot helper. See `OutreachGenerator.generate` for arg semantics."""
    generator = OutreachGenerator(api_key=api_key)
    return generator.generate(
        job=job,
        cv=cv,
        candidate_name=candidate_name,
        contact_name=contact_name,
        contact_role=contact_role,
        cv_excerpt=cv_excerpt,
        voice_notes_md=voice_notes_md,
        candidate_profile_md=candidate_profile_md,
    )
