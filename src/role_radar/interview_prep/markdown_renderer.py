"""Render a PrepDoc to a Markdown document.

Format mirrors the Snowflake prep doc: numbered sections, bold sub-headers,
inline bullets. The LLM emits Markdown for body fields, so this renderer mostly
glues sections together.
"""

from __future__ import annotations

from datetime import datetime

from role_radar.interview_prep.models import (
    BackgroundFit,
    Closing,
    InterviewQuestion,
    PrepDay,
    PrepDoc,
    TopicToKnow,
)


def render_markdown(doc: PrepDoc) -> str:
    """Render a PrepDoc to Markdown."""
    lines: list[str] = []
    h = doc.header

    # Header block
    lines.append(f"# {h.company} — {h.role_title}")
    lines.append("**Interview Preparation Document**")
    bits = [h.location]
    if h.compensation:
        bits.append(h.compensation)
    lines.append(" | ".join(bits))
    gen_date = doc.generated_at or datetime.utcnow()
    lines.append(f"_{h.candidate_name} · {gen_date.strftime('%B %Y')}_")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 1
    lines.append("## 1 / What this role actually is")
    lines.append("")
    lines.append(doc.role_summary_markdown.strip())
    lines.append("")

    # Section 2
    lines.append("## 2 / Topics you must know cold")
    lines.append("")
    lines.append(
        "These are the topics where being unprepared will end the interview "
        "in the first five minutes. Each is summarized to the level you need "
        "to discuss intelligently with the hiring manager and engineering partners."
    )
    lines.append("")
    for topic in doc.topics_to_know:
        lines.extend(_render_topic(topic))

    # Section 3 (optional)
    if doc.strategic_angle_markdown:
        lines.append("## 3 / The strategic angle")
        lines.append("")
        lines.append(doc.strategic_angle_markdown.strip())
        lines.append("")

    # Section 4
    section_n = 4 if doc.strategic_angle_markdown else 3
    lines.append(f"## {section_n} / Likely interview questions and how to approach them")
    lines.append("")
    for q in doc.likely_questions:
        lines.extend(_render_question(q))

    # Section 5
    section_n += 1
    lines.append(f"## {section_n} / How your background maps — and the gaps to address directly")
    lines.append("")
    lines.extend(_render_background_fit(doc.background_fit))

    # Section 6
    section_n += 1
    lines.append(f"## {section_n} / The preparation plan")
    lines.append("")
    for day in doc.prep_plan:
        lines.extend(_render_prep_day(day))

    # Section 7
    section_n += 1
    lines.append(f"## {section_n} / Closing — questions to ask, disqualifiers, the strongest single move")
    lines.append("")
    lines.extend(_render_closing(doc.closing))

    # Sources
    if doc.sources_markdown:
        lines.append("## Sources")
        lines.append("")
        lines.append(doc.sources_markdown.strip())
        lines.append("")

    return "\n".join(lines)


def _render_topic(topic: TopicToKnow) -> list[str]:
    out = [f"### {topic.section_number}  {topic.title}", "", topic.body_markdown.strip(), ""]
    return out


def _render_question(q: InterviewQuestion) -> list[str]:
    out = [
        f"### {q.section_number}  {q.category}",
        "",
        f"**Q:** {q.question}",
        "",
        f"**Approach:** {q.approach}",
        "",
        q.sample_answer_markdown.strip(),
        "",
        f"_Why this works: {q.why_this_works}_",
        "",
    ]
    return out


def _render_background_fit(bf: BackgroundFit) -> list[str]:
    out = ["### What you bring that maps directly", ""]
    out.extend([f"- {item}" for item in bf.what_you_bring])
    out.extend(["", "### The honest gaps", ""])
    out.extend([f"- {item}" for item in bf.honest_gaps])
    out.extend(["", "### How to frame the gap honestly", "", bf.how_to_frame_gap_markdown.strip(), ""])
    return out


def _render_prep_day(day: PrepDay) -> list[str]:
    return [f"### {day.day} — {day.focus}", "", day.items_markdown.strip(), ""]


def _render_closing(c: Closing) -> list[str]:
    out = ["### Three questions to ask, ranked by signal", ""]
    for i, q in enumerate(c.questions_to_ask, 1):
        out.append(f"{i}. {q}")
    out.extend(["", "### Disqualifiers for this specific role", ""])
    out.extend([f"- {d}" for d in c.disqualifiers])
    out.extend(
        [
            "",
            "### The single strongest move you can make",
            "",
            c.strongest_single_move_markdown.strip(),
            "",
        ]
    )
    if c.final_note_markdown:
        out.extend(["### Final note", "", c.final_note_markdown.strip(), ""])
    return out
