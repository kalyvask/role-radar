"""Second-pass critic for generated prep docs.

Inspired by gstack's `/review` + `/codex` pattern: after generation, a separate
LLM call independently audits the doc against the same calibration bar the
generator was asked to hit. The critic receives no special context about the
candidate or the job — it sees only the rendered Markdown — so its judgment is
about doc quality, not about whether the model correctly understood inputs.

The output is a structured ReviewReport: a list of findings with severity, the
specific section/quote, and a suggested fix. Findings get appended to the
generated Markdown as a `## Reviewer notes` section.

Use this when you want belt-and-suspenders quality on a prep doc you're about to
spend 90 minutes rehearsing — the cost is one extra Claude call (~$0.30) and
~30s of latency.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from role_radar.utils.logging import get_logger

logger = get_logger(__name__)

REVIEWER_MODEL = "claude-opus-4-7"
REVIEWER_MAX_TOKENS = 4000


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = Field(description="One of: 'critical', 'major', 'minor'")
    section: str = Field(description="Which section the finding is about, e.g. '4.2 Technical depth'")
    quote: str = Field(description="A short verbatim quote from the doc that illustrates the issue.")
    issue: str = Field(description="What's wrong, in one sentence.")
    suggested_fix: str = Field(description="A specific, actionable fix.")


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_score: int = Field(ge=1, le=10, description="Overall doc quality, 1-10.")
    summary: str = Field(description="2-3 sentence summary of the doc's strengths and weaknesses.")
    findings: list[Finding] = Field(
        description="Specific findings, ordered by severity. Empty list if the doc is already excellent."
    )
    ship_recommendation: str = Field(
        description="One of: 'ship as-is', 'ship with fixes noted', 'regenerate'."
    )


REVIEWER_SYSTEM_PROMPT = """You are a senior PM coach reviewing an interview prep document a colleague is about to use to prepare for a real interview. Your job is to find specific, fixable problems — not to praise.

The bar you're grading against:

**Sample answers must:**
- Use specific numbers (revenue, scale, percentages, latencies, dollar amounts).
- Name technologies, frameworks, models, and architectures by name.
- Be 200-400 words for behavioral, 300-500 for case/system design.
- Flip a perceived weakness into a strength somewhere in the answer.
- Bridge to the target company/role in the closing line.
- Read in the candidate's voice — first person, conversational but tight, no hedging ("I think", "kind of", "maybe").
- For AI system design: name a model selection (XGBoost vs LLM vs rules), draw a 4-layer diagram in words (interaction, orchestration, agents, data), name 3 memory types, walk a metric cascade with dollar math, name failure modes and a 10x scale bottleneck.
- For safety-relevant questions: name ≥3 concrete harm vectors, identify safety gates, appeals path.

**Topics to know cold must:**
- Be specific to the company, not generic. If they're discussing the company's architecture, name the actual technologies (e.g. Envoy not "proxy", PrivateLink not "private connectivity").
- Have a "why this matters for the interview" framing.

**The honest gaps section must:**
- Actually name real gaps, not soft-pedal them.
- Provide a verbatim framing the candidate can use in the room.

**Red flags that mean a doc needs rewrite:**
- Generic answers that could apply to any company.
- "AI magic box" language without naming specific models or architectures.
- Sample answers that say "it worked out" instead of giving a number.
- Behavioral stories that aren't traceable to the candidate's actual CV signals.
- Length-padding paragraphs that don't add information.
- Hedging language in sample answers.

Be direct. If the doc is great, say so and recommend "ship as-is". If it has fixable issues, list them with severity and specific quotes. If it's fundamentally generic or hallucinated, recommend "regenerate".

Output a ReviewReport JSON object. Findings should be ordered by severity (critical first). For each finding, quote the specific text that illustrates the issue."""


def review_prep_doc(
    markdown_doc: str,
    api_key: Optional[str] = None,
    model: str = REVIEWER_MODEL,
) -> ReviewReport:
    """Run a second-pass review on a generated prep doc.

    Args:
        markdown_doc: The full rendered Markdown of the prep doc.
        api_key: Anthropic API key (falls back to ANTHROPIC_API_KEY env var).
        model: Claude model ID (defaults to Opus 4.7).

    Returns:
        A validated ReviewReport.
    """
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "The `anthropic` package is required for prep doc review. "
            "Install with: pip install 'role-radar[interview]'."
        ) from e

    kwargs = {}
    if api_key:
        kwargs["api_key"] = api_key
    elif not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(**kwargs)

    user_prompt = (
        "Review this interview prep document. Be specific, be direct, and ground every "
        "finding in a verbatim quote from the doc.\n\n"
        "---\n\n"
        f"{markdown_doc}"
    )

    logger.info("prep_doc_review_started", doc_chars=len(markdown_doc), model=model)

    response = client.messages.parse(
        model=model,
        max_tokens=REVIEWER_MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=REVIEWER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        output_format=ReviewReport,
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"Reviewer hit max_tokens ({REVIEWER_MAX_TOKENS}).")
    if response.parsed_output is None:
        raise RuntimeError("Reviewer returned no parsed output.")

    report = response.parsed_output
    logger.info(
        "prep_doc_review_completed",
        score=report.overall_score,
        findings=len(report.findings),
        recommendation=report.ship_recommendation,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return report


def render_review_markdown(report: ReviewReport) -> str:
    """Render a ReviewReport as a Markdown section to append to the prep doc."""
    lines = [
        "",
        "---",
        "",
        "## Reviewer notes",
        "",
        f"_Independent second-pass critic. Overall: **{report.overall_score}/10** · "
        f"Recommendation: **{report.ship_recommendation}**_",
        "",
        report.summary.strip(),
        "",
    ]

    if not report.findings:
        lines.append("_No findings. Doc clears the bar as written._")
        return "\n".join(lines)

    by_severity: dict[str, list[Finding]] = {"critical": [], "major": [], "minor": []}
    for f in report.findings:
        by_severity.setdefault(f.severity.lower(), []).append(f)

    for severity in ("critical", "major", "minor"):
        items = by_severity.get(severity, [])
        if not items:
            continue
        lines.append(f"### {severity.title()} ({len(items)})")
        lines.append("")
        for f in items:
            lines.append(f"**{f.section}** — {f.issue}")
            lines.append(f"> {f.quote}")
            lines.append(f"_Fix: {f.suggested_fix}_")
            lines.append("")

    return "\n".join(lines)
