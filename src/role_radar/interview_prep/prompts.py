"""Prompt assembly for the interview prep generator.

Two prompts:
- `build_system_prompt()` — the static, cacheable context (role, frameworks,
  calibrations, company playbook). Stable across many jobs at the same company.
- `build_user_prompt()` — the per-job specifics (CV signals, job posting).
  Volatile, uncached.

The split matters: the system prompt is what gets cached. Putting per-job
volatile content in the system block would invalidate the cache on every call.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from role_radar.interview_prep.data import (
    find_company_playbook,
    load_calibrations,
    load_frameworks,
    load_laws,
)
from role_radar.models import CVSignals, Job


SYSTEM_PROMPT_INTRO = """You are an interview prep coach who writes opinionated, candidate-tailored prep documents for senior PM interviews. Your output is a structured prep doc — not generic advice, not a checklist, not a "here are 10 tips" listicle.

The prep doc you write should match the depth and voice of a senior advisor who has read the JD literally, triangulated from the company's public engineering blog and product surface, and knows the candidate's CV cold. Concrete > abstract. Specific > generic. Honest > flattering.

Quality bar:
- Every claim about the role must be grounded in the JD or in widely-known public information about the company. Do not fabricate org charts, team structures, or specific people.
- Every sample answer must use real numbers, named technologies, named frameworks, and the candidate's actual experiences from their CV. Vague answers fail.
- When the candidate has a real gap for the role, name it directly. Don't whitewash. The honest framing of a gap beats pretending it isn't there.
- Sample answers are written in the candidate's voice, first person, the way they would actually speak. Conversational but tight. ~150 words/min, so a 2-minute answer is ~300 words.
- Use the company's actual product surface, terminology, and strategic emphasis from the JD. If the JD mentions "AI workloads" or "platform parity" or "multi-cloud," develop a real view on each.

Voice notes:
- Direct, not deferential. Avoid "I think," "kind of," "maybe," "sort of," "probably."
- No filler ("basically," "essentially," "fundamentally").
- Don't hedge unless hedging is the point.
- No emoji. No "let me know if you have questions."
"""


SYSTEM_PROMPT_FRAMEWORKS = """## Frameworks the candidate uses

These are the candidate's actual interview frameworks — pulled from their personal interview prep system. Use them when constructing sample answers, especially for AI system design and metrics questions.

### DASME — AI System Design framework
{dasme}

### Model selection — when to use what
For any AI system design answer, the candidate should explicitly justify model choices using this table:
{model_selection}

### SIGNAL — Metric cascade
For any AI metrics question, layer 1 (model) → layer 2 (UX) → layer 3 (business). Translate one metric delta into dollars.
{signal_cascade}

### Anti-patterns to avoid in answers
{anti_patterns}

### Safety checklist (raise these proactively in safety-relevant answers)
{safety_checklist}

### The 3 Laws of interview delivery
{three_laws}

### The 8 dimensions answers are graded on
{eight_dimensions}
"""


SYSTEM_PROMPT_CALIBRATIONS = """## Calibration: the answer-quality bar

These are paired weak/strong examples that define the bar for this candidate's answers. When you write a sample answer in the prep doc, it should match the *strong* version's caliber: specific metrics, named technologies, weakness-flipping, memorable detail, and a bridge to the target role.

{calibrations}

The pattern: strong answers combine specific numbers, named technologies/frameworks, an honest acknowledgment of a weakness flipped into a strength, and a concrete bridge to the target role. Weak answers hedge, generalize, and use vague terms.
"""


SYSTEM_PROMPT_COMPANY_PLAYBOOK = """## Company-specific playbook

This is what the candidate's interview prep system knows about interviews at this specific company. Treat it as ground truth and weave it into the prep doc.

{playbook}
"""


SYSTEM_PROMPT_NO_PLAYBOOK = """## Company-specific playbook

No company-specific playbook is available for this company in the candidate's prep system. Build the prep doc from public information about the company (engineering blog, product surface, strategic emphasis from earnings, well-known interview style if it's known) and the JD.
"""


SYSTEM_PROMPT_INSTRUCTIONS = """## Output instructions

You will emit a single PrepDoc object matching the provided JSON schema. Section guidance:

1. **Header**: company, role title, location, comp range (parse from job posting if present, else null), candidate name.

2. **Role summary** (`role_summary_markdown`): 2-4 paragraphs. What this role *actually* is — read the JD literally and triangulate from the company's product surface. Then a Markdown bullet list of the product surface broken into 5-8 components. Then (if applicable) 1 paragraph on how this role differs from adjacent roles at the company.

3. **Topics to know cold** (`topics_to_know`): 3-6 topics. These are the things where being unprepared ends the interview in the first 5 minutes. Each topic gets a substantive `body_markdown` section (200-400 words) with:
   - A "Key facts" sub-section (bullet list of the specific, technical things to know)
   - A "Why this matters for the interview" sub-section (1 paragraph on the bar of fluency expected)
   For an infra/platform role, this is the company's architecture and the relevant primitives. For an apps role, this is the product surface and the customer JTBD.

4. **Strategic angle** (`strategic_angle_markdown`, optional): If the JD has a clear strategic emphasis (AI, infra shift, multi-cloud, new market), develop the candidate's view on it in 2-4 paragraphs. Otherwise null.

5. **Likely questions** (`likely_questions`): 5-7 questions covering different categories. For each, write a *real* sample answer in the candidate's voice using their CV. Categories to cover (pick the relevant ones, not all):
   - Why this team / why this role
   - Technical depth — describe the architecture / a specific primitive
   - Product sense — improve a specific surface
   - Strategy — prioritize work for a strategic emphasis
   - Behavioral — a story from the candidate's CV that maps to this role
   - Trade-off — a real tension this PM faces
   - Customer empathy — the hardest customer conversation

6. **Background fit** (`background_fit`): What the candidate brings that maps to this role. Honest gaps. The exact verbatim framing they should use to address the largest gap in the room.

7. **Prep plan** (`prep_plan`): A multi-day plan (5-7 days) with concrete reading and rehearsal tasks. Name specific blog posts, docs, books, or candidate stories to rehearse.

8. **Closing** (`closing`): 3 questions to ask the interviewer ranked by signal. 3-5 disqualifiers specific to this role. The single strongest move the candidate can make in the room.

9. **Sources** (`sources_markdown`, optional): Bullet list of specific sources to read.

Format hygiene:
- Markdown bodies use standard Markdown — `**bold**`, bullet lists with `-`, code with backticks. No HTML.
- No "I'll write this section now" preambles — emit the structured object directly.
- Don't pad. If a section can be 200 words instead of 400, do 200.
"""


def _format_dasme(frameworks: dict[str, Any]) -> str:
    rows = []
    for phase in frameworks.get("dasme", []):
        rows.append(
            f"- **{phase['letter']} — {phase['name']}** ({phase['time_share']}, {phase['minutes']}): "
            f"{phase['body']}"
        )
    return "\n".join(rows)


def _format_model_selection(frameworks: dict[str, Any]) -> str:
    rows = ["| Task | Use | Why |", "|---|---|---|"]
    for r in frameworks.get("model_selection", []):
        rows.append(f"| {r['task']} | {r['use']} | {r['why']} |")
    return "\n".join(rows)


def _format_signal_cascade(frameworks: dict[str, Any]) -> str:
    out = []
    for layer in frameworks.get("signal_cascade", []):
        out.append(f"\n**{layer['name']}: {layer['label']}**")
        for ex in layer.get("examples", []):
            out.append(f"- {ex['kind']}: {ex['metrics']}")
    return "\n".join(out)


def _format_anti_patterns(frameworks: dict[str, Any]) -> str:
    rows = []
    for ap in frameworks.get("anti_patterns", []):
        rows.append(f"- **{ap['title']}** — Fail: {ap['fail']} Fix: {ap['fix']}")
    return "\n".join(rows)


def _format_safety_checklist(frameworks: dict[str, Any]) -> str:
    rows = []
    for s in frameworks.get("safety_checklist", []):
        rows.append(f"- **{s['question']}** {s['body']}")
    return "\n".join(rows)


def _format_three_laws(laws: dict[str, Any]) -> str:
    rows = []
    for law in laws.get("three_laws", []):
        rows.append(f"{law['number']}. **{law['title']}** {law['body']}")
    return "\n".join(rows)


def _format_eight_dimensions(laws: dict[str, Any]) -> str:
    rows = []
    for d in laws.get("eight_dimensions", []):
        rows.append(f"- **{d['name']}** — {d['detail']}")
    return "\n".join(rows)


def _format_calibrations(calibrations: dict[str, Any]) -> str:
    rows = []
    for c in calibrations.get("calibrations", []):
        rows.append(f"\n### Q: {c['question']}")
        if c.get("context"):
            rows.append(f"_Context: {c['context']}_")
        weak = c["weak"]
        strong = c["strong"]
        rows.append(f"\n**WEAK ({weak['score']}):**")
        rows.append(f"> {weak['response']}")
        rows.append(f"_Flaws: {'; '.join(weak['flaws'])}_")
        rows.append(f"\n**STRONG ({strong['score']}):**")
        rows.append(f"> {strong['response']}")
        rows.append(f"_Strengths: {'; '.join(strong['strengths'])}_")
    return "\n".join(rows)


def _format_company_playbook(playbook: dict[str, Any]) -> str:
    out = [
        f"### {playbook['name']}",
        f"**One-liner:** {playbook['one_liner']}",
    ]
    if playbook.get("tc"):
        out.append(f"**TC:** {playbook['tc']}")
    if playbook.get("pass_rate"):
        out.append(f"**Pass rate:** {playbook['pass_rate']}")
    out.append(f"\n**Structure:** {playbook['structure']}")
    out.append(f"\n**What they test:**")
    for item in playbook.get("what_they_test", []):
        out.append(f"- {item}")
    out.append(f"\n**Where system thinking shows up:** {playbook['where_system_thinking']}")
    out.append(f"\n**Red flag:** {playbook['red_flag']}")
    out.append(f"\n**Sample questions:**")
    for q in playbook.get("sample_questions", []):
        out.append(f"- {q}")
    return "\n".join(out)


def build_system_prompt(company_name: str) -> str:
    """Build the static, cacheable system prompt.

    Includes: intro, frameworks, calibrations, and (if available) the company
    playbook. This text is stable across many jobs at the same company, so the
    cache hit rate should be high when generating prep for multiple jobs.
    """
    frameworks = load_frameworks()
    laws = load_laws()
    calibrations = load_calibrations()
    playbook = find_company_playbook(company_name)

    parts = [SYSTEM_PROMPT_INTRO]

    parts.append(
        SYSTEM_PROMPT_FRAMEWORKS.format(
            dasme=_format_dasme(frameworks),
            model_selection=_format_model_selection(frameworks),
            signal_cascade=_format_signal_cascade(frameworks),
            anti_patterns=_format_anti_patterns(frameworks),
            safety_checklist=_format_safety_checklist(frameworks),
            three_laws=_format_three_laws(laws),
            eight_dimensions=_format_eight_dimensions(laws),
        )
    )

    parts.append(SYSTEM_PROMPT_CALIBRATIONS.format(calibrations=_format_calibrations(calibrations)))

    if playbook:
        parts.append(SYSTEM_PROMPT_COMPANY_PLAYBOOK.format(playbook=_format_company_playbook(playbook)))
    else:
        parts.append(SYSTEM_PROMPT_NO_PLAYBOOK)

    parts.append(SYSTEM_PROMPT_INSTRUCTIONS)

    return "\n\n".join(parts)


def _format_cv_signals(cv: CVSignals) -> str:
    """Render CV signals into a structured block for the user prompt."""
    out = []
    if cv.recent_titles:
        out.append(f"**Recent titles:** {', '.join(cv.recent_titles[:3])}")
    if cv.inferred_seniority:
        out.append(f"**Inferred seniority:** {cv.inferred_seniority}")
    if cv.years_experience:
        out.append(f"**Years experience:** {cv.years_experience}")
    if cv.companies:
        out.append(f"**Past companies:** {', '.join(cv.companies[:8])}")
    if cv.skills:
        out.append(f"**Skills extracted:** {', '.join(cv.skills[:25])}")
    if cv.domains:
        out.append(f"**Domains:** {', '.join(cv.domains)}")
    if cv.education:
        out.append(f"**Education:** {', '.join(cv.education[:3])}")
    return "\n".join(out)


def _format_job(job: Job) -> str:
    """Render the job posting into a structured block."""
    out = [
        f"**Company:** {job.company}",
        f"**Title:** {job.title}",
        f"**Location:** {job.location.format()}",
    ]
    if job.department:
        out.append(f"**Department:** {job.department}")
    if job.seniority:
        out.append(f"**Seniority:** {job.seniority}")
    if job.salary:
        out.append(f"**Compensation:** {job.salary.format()}")
    if job.posted_date:
        out.append(f"**Posted:** {job.posted_date.strftime('%Y-%m-%d')}")
    out.append(f"**Apply URL:** {job.apply_url}")
    if job.description:
        out.append(f"\n**Job description:**\n{job.description}")
    return "\n".join(out)


def build_user_prompt(
    job: Job,
    cv: CVSignals,
    candidate_name: str,
    cv_excerpt: Optional[str] = None,
) -> str:
    """Build the per-job, volatile user prompt.

    Args:
        job: The job posting.
        cv: Extracted signals from the candidate's CV.
        candidate_name: Candidate's name for the doc header.
        cv_excerpt: Optional raw CV text excerpt (capped at ~3K chars) for richer
            grounding in the candidate's actual experiences.
    """
    parts = [
        f"Generate a prep doc for {candidate_name} for the following role.",
        "",
        "## The job",
        _format_job(job),
        "",
        "## Candidate signals (from CV)",
        _format_cv_signals(cv),
    ]

    if cv_excerpt:
        capped = cv_excerpt.strip()
        if len(capped) > 3500:
            capped = capped[:3500] + "\n[... truncated]"
        parts.extend(["", "## Candidate CV excerpt", "```", capped, "```"])

    parts.extend(
        [
            "",
            "## Task",
            "Emit a single PrepDoc matching the provided schema.",
            "",
            "Ground every sample answer in the candidate's actual experience above. If the "
            "candidate's background has a clear gap for this specific role, name it directly "
            "in the background_fit section and provide the exact framing they should use.",
        ]
    )

    return "\n".join(parts)
