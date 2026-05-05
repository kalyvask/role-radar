"""Anthropic-backed generator for the interview prep document.

Uses Claude Opus 4.7 with:
- Adaptive thinking (Opus 4.7 default; required syntax for the model)
- effort=high (recommended minimum for intelligence-sensitive work)
- Structured output via `messages.parse()` (Pydantic-validated)
- Prompt caching on the system prompt (frameworks/calibrations/playbook are
  stable across jobs at the same company)

Gotchas baked in:
- Opus 4.7 rejects `temperature`, `top_p`, `top_k`, and `budget_tokens` (400).
- The system prompt is passed as a list of one text block with cache_control,
  so the prefix matches across jobs at the same company. Keep volatile content
  (CV, job posting) in the user message.
- max_tokens is set high (16000) because a stop_reason of "max_tokens" mid-doc
  yields invalid JSON that fails the Pydantic parse.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from role_radar.interview_prep.models import PrepDoc
from role_radar.interview_prep.prompts import build_system_prompt, build_user_prompt
from role_radar.models import CVSignals, Job
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


MODEL_ID = "claude-opus-4-7"
MAX_TOKENS = 16000


class PrepGenerationError(RuntimeError):
    """Raised when the generator fails to produce a valid prep doc."""


class InterviewPrepGenerator:
    """Generates a structured prep doc by calling Claude.

    Holds an Anthropic client and a few generation defaults. Stateless across
    calls otherwise — safe to reuse for many jobs in the same process.
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
            raise PrepGenerationError(
                "The `anthropic` package is required for interview prep generation. "
                "Install with: pip install 'role-radar[interview]' or `pip install anthropic`."
            ) from e

        # Use explicit key if provided; otherwise rely on ANTHROPIC_API_KEY env var
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif not os.getenv("ANTHROPIC_API_KEY"):
            raise PrepGenerationError(
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
        cv_excerpt: Optional[str] = None,
    ) -> PrepDoc:
        """Generate a prep doc for a single job.

        Args:
            job: The job posting.
            cv: Parsed CV signals.
            candidate_name: Candidate name for the doc header.
            cv_excerpt: Optional raw CV text (will be capped to ~3.5K chars).

        Returns:
            A validated `PrepDoc` instance.

        Raises:
            PrepGenerationError: on API errors, refusals, or schema failures.
        """
        system_prompt = build_system_prompt(job.company)
        user_prompt = build_user_prompt(
            job=job, cv=cv, candidate_name=candidate_name, cv_excerpt=cv_excerpt
        )

        logger.info(
            "interview_prep_generation_started",
            company=job.company,
            title=job.title,
            model=self._model,
            system_prompt_chars=len(system_prompt),
            user_prompt_chars=len(user_prompt),
        )

        try:
            # System prompt as a list of text blocks lets us mark the static
            # context as cacheable. Per-job content lives in the user message.
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
                output_format=PrepDoc,
            )
        except self._anthropic_module.APIStatusError as e:
            raise PrepGenerationError(
                f"Anthropic API error ({e.status_code}): {e.message}"
            ) from e
        except self._anthropic_module.APIConnectionError as e:
            raise PrepGenerationError(f"Network error reaching Anthropic API: {e}") from e

        if response.stop_reason == "refusal":
            raise PrepGenerationError(
                "Claude refused to generate a prep doc for this job. "
                "This is unusual — check the job description for content that may have triggered a safety filter."
            )

        if response.stop_reason == "max_tokens":
            raise PrepGenerationError(
                f"Hit max_tokens ({self._max_tokens}) before the doc completed. "
                "Increase max_tokens or simplify the request."
            )

        prep_doc = response.parsed_output
        if prep_doc is None:
            raise PrepGenerationError(
                "Anthropic returned a response with no parsed_output. "
                "This usually means the model output didn't match the schema."
            )

        prep_doc.generated_at = datetime.utcnow()

        usage = response.usage
        logger.info(
            "interview_prep_generation_completed",
            company=job.company,
            title=job.title,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        )

        return prep_doc


def generate_prep_for_job(
    job: Job,
    cv: CVSignals,
    candidate_name: str,
    output_dir: Path,
    cv_excerpt: Optional[str] = None,
    api_key: Optional[str] = None,
    write_docx: bool = True,
) -> tuple[PrepDoc, Path, Optional[Path]]:
    """Generate a prep doc and write it to disk as Markdown (and optionally DOCX).

    Returns:
        (prep_doc, markdown_path, docx_path_or_none)
    """
    # Late imports to avoid hard dep on python-docx for callers that only want Markdown
    from role_radar.interview_prep.markdown_renderer import render_markdown
    from role_radar.interview_prep.docx_renderer import render_docx, DocxRenderError

    generator = InterviewPrepGenerator(api_key=api_key)
    prep_doc = generator.generate(job=job, cv=cv, candidate_name=candidate_name, cv_excerpt=cv_excerpt)

    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = _safe_filename(job)
    md_path = output_dir / f"{base_name}.md"
    md_path.write_text(render_markdown(prep_doc), encoding="utf-8")
    logger.info("interview_prep_markdown_written", path=str(md_path))

    docx_path: Optional[Path] = None
    if write_docx:
        try:
            docx_path = output_dir / f"{base_name}.docx"
            render_docx(prep_doc, docx_path)
            logger.info("interview_prep_docx_written", path=str(docx_path))
        except DocxRenderError as e:
            logger.warning("interview_prep_docx_failed", error=str(e))
            docx_path = None

    return prep_doc, md_path, docx_path


def _safe_filename(job: Job) -> str:
    """Build a filesystem-safe base filename for the prep doc."""
    import re

    company_slug = re.sub(r"[^a-zA-Z0-9]+", "_", job.company.lower()).strip("_")
    title_slug = re.sub(r"[^a-zA-Z0-9]+", "_", job.title.lower()).strip("_")[:60]
    return f"{company_slug}__{title_slug}__prep"
