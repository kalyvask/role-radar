"""Prompt assembly for the outreach generator.

Two prompts:
- `build_system_prompt()` — voice/quality rules. Stable across many jobs, so
  cacheable on the API side. Optional candidate profile is appended.
- `build_user_prompt()` — per-job specifics (job posting, candidate signals).

The split matters: the system block is what gets cached. Putting per-job
volatile content in the system block would invalidate the cache on every call.
"""

from __future__ import annotations

from typing import Optional

from role_radar.models import CVSignals, Job


# Default voice rules. These match the writing style typical of senior
# operators sending cold notes — direct, specific, no jargon. Edit for your
# own voice or pass `voice_notes_md` to override.
DEFAULT_VOICE_NOTES = """## Voice rules

- Direct, not deferential. Plain verbs.
- No em-dashes anywhere. Use periods or parentheses.
- Avoid: "not just X also Y", "I hope this finds you well", "circle back",
  "synergy", "passionate about", "moving the needle", "leverage".
- Don't hedge unless hedging is the point. No "I think", "kind of", "maybe".
- Fragments are fine in personal lines.
- Numbers beat adjectives. "Cut p95 latency 40%" beats "improved performance significantly".
- Honest beats clever. Name a real fit; don't fabricate.
- Short sentences. Aim for sentence-level concreteness.

## Cold-email mechanics (S.H.I.T. - Specific, Honest, Interesting, Targeted)

- **Specific**: Name the team, the surface, the post on their blog you read.
  Generic "I admire your work" gets deleted.
- **Honest**: Don't oversell. If you have a gap, name it once and move on.
- **Interesting**: One unexpected hook (a story, a number, a parallel) per
  email. The reader should feel they've learned one thing about you.
- **Targeted**: Tailor to THIS job at THIS company. If the same email could
  go to a different company, it's not ready.

## Structural rules

- 100-200 words. 150 is a good target.
- Opener: a specific hook. Not "I hope this finds you well." Not "I'm reaching out about..."
  Examples that work:
    - "Saw the [role] opening on [day]. Two things that map:"
    - "Your post on [topic] hit something I lived through at [company]."
- One paragraph on fit (specific). One paragraph on a low-pressure ask.
- Sign-off: just the candidate's first name. No "Best regards" or "Looking forward."
- Subject line: 4-7 words, references the role or a hook. No "Quick question."
"""


SYSTEM_PROMPT_INTRO = """You write cold outreach emails for a senior PM candidate applying to AI companies. Output is one email draft per call: a subject line, a body, a brief rationale, and an honest self-rating.

The email is sent before or alongside a formal application. Its job is to make the candidate feel like a real person to a hiring manager or recruiter, not to replace the application. So it has to be specific, short, and honest.

You are NOT writing a cover letter. You are writing a 150-word note that a busy hiring manager would read in 20 seconds and remember.

Quality bar:
- Every claim about fit must be grounded in the candidate's CV or the JD. Do not fabricate experiences.
- Every reference to the company must be grounded in publicly known facts about that company. Do not invent products, blog posts, people, or strategy.
- If the candidate has a real gap for the role, you may name it directly in one short clause. Don't whitewash, don't overclaim.
- The opener must be specific. The first sentence is the most important.
- The ask at the end must be low-pressure ("worth a 15-min chat?", "happy to share more if useful"), not "let me know when we can interview."
"""


SYSTEM_PROMPT_INSTRUCTIONS = """## Output instructions

You will emit a single OutreachDraft object matching the provided JSON schema.

- `subject`: 4-7 words. Specific. References the role or a concrete hook.
- `body`: 100-200 words. Plain text only, no Markdown, no HTML. No links unless the JD provides one. End with the candidate's first name only.
- `rationale`: 1-2 sentences explaining why this draft will work for this specific candidate-job pair. Not shown to the recipient. Be specific about what makes this email match.
- `self_rating`: 1-10 honest score. Inflated ratings make the system useless. If you'd be embarrassed to send it, rate it below 5.

Format hygiene:
- No em-dashes (—) anywhere in subject or body. Use periods or parentheses instead.
- No emoji.
- No "Hi [Name]," if you don't know the recipient — open with the hook directly.
- No P.S. or "looking forward to hearing from you."
"""


def build_system_prompt(
    voice_notes_md: Optional[str] = None,
    candidate_profile_md: Optional[str] = None,
) -> str:
    """Build the static, cacheable system prompt.

    Args:
        voice_notes_md: Optional Markdown overriding DEFAULT_VOICE_NOTES.
        candidate_profile_md: Optional Markdown profile of the candidate
            (career arc, distinctive credentials, signature stories). When
            provided, the generator can pull richer hooks than the CV alone.
    """
    parts = [SYSTEM_PROMPT_INTRO, voice_notes_md or DEFAULT_VOICE_NOTES]

    if candidate_profile_md:
        parts.append("## Candidate profile\n\n" + candidate_profile_md.strip())

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
    contact_name: Optional[str] = None,
    contact_role: Optional[str] = None,
    cv_excerpt: Optional[str] = None,
) -> str:
    """Build the per-job user prompt.

    Args:
        job: The job posting.
        cv: Extracted signals from the candidate's CV.
        candidate_name: Candidate's first name for the sign-off.
        contact_name: Optional recipient name. If None, opener skips the salutation.
        contact_role: Optional recipient role label (e.g. "recruiter", "VP Eng").
        cv_excerpt: Optional raw CV text excerpt (capped at ~3K chars).
    """
    parts = [
        f"Draft a cold outreach email for {candidate_name} for the following role.",
        "",
        "## The job",
        _format_job(job),
        "",
        "## Candidate signals (from CV)",
        _format_cv_signals(cv),
    ]

    if contact_name or contact_role:
        parts.extend(["", "## Recipient"])
        if contact_name:
            parts.append(f"**Name:** {contact_name}")
        if contact_role:
            parts.append(f"**Role:** {contact_role}")
    else:
        parts.extend([
            "",
            "## Recipient",
            "**Unknown.** No contact name. Open with the hook directly, no salutation.",
        ])

    if cv_excerpt:
        capped = cv_excerpt.strip()
        if len(capped) > 3000:
            capped = capped[:3000] + "\n[... truncated]"
        parts.extend(["", "## Candidate CV excerpt", "```", capped, "```"])

    parts.extend(
        [
            "",
            "## Task",
            (
                "Emit a single OutreachDraft matching the provided schema. "
                "Ground every claim about fit in the candidate's CV. Ground every claim "
                "about the company in the JD or widely-known public facts. If the candidate "
                "has a clear gap for this role, you may name it once briefly. Sign off with "
                f"just '{candidate_name.split()[0]}'."
            ),
        ]
    )

    return "\n".join(parts)
