"""Anthropic-backed generator for the company review document.

Uses Claude with the server-side web_search tool. The model researches the
company live (typically 15-25 search queries) and emits a Markdown document
following the section template defined in `prompts.py`.

Why this is structured differently from `interview_prep`:
- Interview prep uses Claude's structured-output API (`messages.parse()`) with
  a Pydantic schema. That doesn't compose cleanly with server-side tools.
- Company review needs current web data (funding, valuation, sentiment) which
  is not in training data — so it must use web_search.

The trade-off: the output is Markdown (validated by template adherence + a
machine-readable signal line at the bottom), not Pydantic-validated.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from role_radar.company_review.models import CompanyReviewDoc, CompanyReviewSource
from role_radar.company_review.prompts import build_system_prompt, build_user_prompt
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


# Sonnet is the right pick here: long-context research + many tool calls. Opus
# would be slower and more expensive without notably better facts. If quality
# regresses, override via the `model` parameter.
MODEL_ID = "claude-sonnet-4-5"
MAX_TOKENS = 16000
MAX_WEB_SEARCH_USES = 25


class CompanyReviewGenerationError(RuntimeError):
    """Raised when the generator fails to produce a valid review doc."""


# Maps the model's signal sentinel to the user-facing label.
_SIGNAL_LABELS = {
    "strong-apply": "Strong apply",
    "apply-with-caveats": "Apply with caveats",
    "pass": "Pass",
    "inconclusive": "Inconclusive",
}

_SIGNAL_RE = re.compile(r"<!--\s*review-signal:\s*([\w-]+)\s*-->", re.IGNORECASE)
_TLDR_RE = re.compile(r"\*\*TL;DR\*\*\s*\n\n(.+?)(?:\n\n##|$)", re.DOTALL)


class CompanyReviewGenerator:
    """Generates a structured company review by calling Claude with web_search."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL_ID,
        max_tokens: int = MAX_TOKENS,
        max_web_searches: int = MAX_WEB_SEARCH_USES,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise CompanyReviewGenerationError(
                "The `anthropic` package is required for company review generation. "
                "Install with: pip install anthropic."
            ) from e

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif not os.getenv("ANTHROPIC_API_KEY"):
            raise CompanyReviewGenerationError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env or pass api_key explicitly."
            )

        self._client = anthropic.Anthropic(**kwargs)
        self._anthropic_module = anthropic
        self._model = model
        self._max_tokens = max_tokens
        self._max_web_searches = max_web_searches

    def generate(
        self,
        company: str,
        homepage: Optional[str] = None,
        careers_url: Optional[str] = None,
        category: Optional[str] = None,
        funding_amount_m: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> CompanyReviewDoc:
        """Generate a company review doc."""
        system_prompt = build_system_prompt()
        user_prompt = build_user_prompt(
            company=company,
            homepage=homepage,
            careers_url=careers_url,
            category=category,
            funding_amount_m=funding_amount_m,
            notes=notes,
        )

        logger.info(
            "company_review_generation_started",
            company=company,
            model=self._model,
            system_prompt_chars=len(system_prompt),
            user_prompt_chars=len(user_prompt),
        )

        started_at = time.time()
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": self._max_web_searches,
                    }
                ],
            )
        except self._anthropic_module.APIStatusError as e:
            raise CompanyReviewGenerationError(
                f"Anthropic API error ({e.status_code}): {e.message}"
            ) from e
        except self._anthropic_module.APIConnectionError as e:
            raise CompanyReviewGenerationError(
                f"Network error reaching Anthropic API: {e}"
            ) from e

        if response.stop_reason == "refusal":
            raise CompanyReviewGenerationError(
                "Claude refused to generate a review for this company. "
                "Check the system prompt or company name for content that may have triggered a safety filter."
            )

        if response.stop_reason == "max_tokens":
            logger.warning(
                "company_review_max_tokens_hit",
                company=company,
                max_tokens=self._max_tokens,
            )

        markdown_chunks: list[str] = []
        sources: list[CompanyReviewSource] = []
        search_queries: list[str] = []
        web_search_count = 0

        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                markdown_chunks.append(block.text)
            elif block_type == "server_tool_use":
                # Track each web search the model issued
                if getattr(block, "name", None) == "web_search":
                    web_search_count += 1
                    inp = getattr(block, "input", None) or {}
                    q = inp.get("query") if isinstance(inp, dict) else None
                    if q:
                        search_queries.append(q)
            elif block_type == "web_search_tool_result":
                # Capture sources from search results so we can render a Sources block
                results = getattr(block, "content", None) or []
                for r in results:
                    url = getattr(r, "url", None)
                    title = getattr(r, "title", None)
                    if url and not any(s.url == url for s in sources):
                        sources.append(CompanyReviewSource(url=url, title=title))

        markdown = "\n".join(markdown_chunks).strip()
        if not markdown:
            raise CompanyReviewGenerationError(
                "Anthropic returned an empty response. The model may have refused mid-stream."
            )

        # The model sometimes emits "I'll research..." preamble before the H1
        # despite the prompt telling it not to. Strip everything up to the H1.
        h1_idx = markdown.find("\n# ")
        if h1_idx > 0:
            markdown = markdown[h1_idx + 1:].lstrip()
        elif not markdown.startswith("# "):
            # No H1 at all — log warning but keep content
            logger.warning(
                "company_review_no_h1",
                company=company,
                head=markdown[:200],
            )

        # Parse the machine-readable signal line + TL;DR
        overall_signal = None
        signal_match = _SIGNAL_RE.search(markdown)
        if signal_match:
            raw = signal_match.group(1).strip().lower()
            overall_signal = _SIGNAL_LABELS.get(raw, raw)

        headline_summary = None
        tldr_match = _TLDR_RE.search(markdown)
        if tldr_match:
            headline_summary = tldr_match.group(1).strip()

        usage = response.usage
        duration = time.time() - started_at
        doc = CompanyReviewDoc(
            company=company,
            markdown=markdown,
            overall_signal=overall_signal,
            headline_summary=headline_summary,
            sources=sources,
            search_queries=search_queries,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            web_search_count=web_search_count,
            duration_seconds=round(duration, 1),
            model=self._model,
            generated_at=datetime.utcnow(),
        )

        logger.info(
            "company_review_generation_completed",
            company=company,
            duration_s=round(duration, 1),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            web_search_count=web_search_count,
            sources_captured=len(sources),
            signal=overall_signal,
        )

        return doc


def generate_review_for_company(
    company: str,
    output_dir: Path,
    homepage: Optional[str] = None,
    careers_url: Optional[str] = None,
    category: Optional[str] = None,
    funding_amount_m: Optional[float] = None,
    notes: Optional[str] = None,
    api_key: Optional[str] = None,
) -> tuple[CompanyReviewDoc, Path]:
    """Generate a company review and write it to disk as Markdown.

    Returns:
        (review_doc, markdown_path)
    """
    from role_radar.company_review.markdown_renderer import render_markdown

    generator = CompanyReviewGenerator(api_key=api_key)
    doc = generator.generate(
        company=company,
        homepage=homepage,
        careers_url=careers_url,
        category=category,
        funding_amount_m=funding_amount_m,
        notes=notes,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = _safe_filename(company)
    md_path = output_dir / f"{base_name}.md"
    md_path.write_text(render_markdown(doc), encoding="utf-8")
    logger.info("company_review_markdown_written", path=str(md_path))

    return doc, md_path


def _safe_filename(company: str) -> str:
    """Build a filesystem-safe base filename for the review doc."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", company.lower()).strip("_")
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return f"{slug}__review_{stamp}"
