"""Render a CompanyReviewDoc to Markdown.

The LLM emits Markdown directly via the web_search workflow, so this
renderer just prepends a metadata block (verdict badge, generation time,
token usage) and appends a sources block built from captured search results
that the model didn't already cite. The model's own Sources section stays
intact.
"""

from __future__ import annotations

from datetime import datetime

from role_radar.company_review.models import CompanyReviewDoc


def render_markdown(doc: CompanyReviewDoc) -> str:
    """Wrap the raw LLM-emitted Markdown with metadata + extra sources."""
    lines: list[str] = []

    # The LLM is instructed to start with `# {Company} — Company Review`. We
    # prepend a metadata block above that header so the original document
    # stays as-is and the verdict is visible at a glance.
    gen = doc.generated_at or datetime.utcnow()
    meta_bits = [f"_Generated {gen.strftime('%B %d, %Y at %H:%M UTC')}"]
    if doc.model:
        meta_bits.append(f"by {doc.model}")
    if doc.duration_seconds:
        meta_bits.append(f"in {doc.duration_seconds:.0f}s")
    if doc.web_search_count:
        meta_bits.append(f"with {doc.web_search_count} web searches")
    meta_line = " ".join(meta_bits) + "_"

    if doc.overall_signal:
        lines.append(f"> **Verdict:** {doc.overall_signal}")
        lines.append("")
    lines.append(meta_line)
    lines.append("")
    lines.append("---")
    lines.append("")

    # Append the model's Markdown verbatim. It already starts with the H1.
    lines.append(doc.markdown.strip())

    # If the model's Sources block is missing or thin, append captured sources
    # the SDK gave us from web_search results that didn't make it into the prose.
    rendered = "\n".join(lines)
    if "## Sources" not in rendered and doc.sources:
        lines.append("")
        lines.append("## Sources")
        lines.append("")
        for src in doc.sources:
            label = src.title or src.url
            lines.append(f"- [{label}]({src.url})")

    return "\n".join(lines)
