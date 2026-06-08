"""Storage-backed helpers for the connections layer.

Keeps the CLI, web UI, and outreach drafter from each re-implementing the
"load connections, build matcher, look up a company's investors, format a
prompt block" plumbing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from role_radar.connections.matcher import IntroMatcher
from role_radar.connections.models import Connection, IntroTier, WarmIntro
from role_radar.connections.normalize import normalize_company

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from role_radar.storage import Storage


def load_connections(storage: "Storage") -> list[Connection]:
    """Load every imported connection from storage."""
    return [Connection.from_row(row) for row in storage.get_all_connections()]


def build_matcher(storage: "Storage") -> IntroMatcher:
    """Build an `IntroMatcher` over the user's stored network."""
    return IntroMatcher(load_connections(storage))


def backers_for(storage: "Storage", company_name: str) -> list[str]:
    """Return the investor list for a company, matched by normalized name.

    Reads `Company.backed_by`, which role-radar populates from portfolios.csv
    `vc_backers` and VC scoring. Returns [] if the company isn't stored or has
    no recorded investors (Tier 2 simply won't fire in that case).
    """
    target = normalize_company(company_name)
    if not target:
        return []
    for company in storage.get_all_companies():
        if normalize_company(company.name) == target and company.backed_by:
            return list(company.backed_by)
    return []


# Honesty guard: a LinkedIn connection is a *potential* lead, not a confirmed
# relationship. Naming someone the candidate doesn't actually know — or implying
# an endorsement — backfires the moment the recipient checks. The drafter must
# surface a shared connection only as an honest, verifiable fact.
_OUTREACH_GUIDANCE = (
    "Guidance for using these:\n"
    "- These are surfaced from a network import. The candidate has NOT confirmed "
    "a real relationship with any of them. Do NOT write \"X suggested I reach out\" "
    "or imply an endorsement.\n"
    "- If you reference one, do it honestly and verifiably: \"I see we're both "
    "connected to {name}\" or \"I noticed {name} on your team.\" That claim is the "
    "candidate's to make, so keep it factual.\n"
    "- Name at most ONE connection, the most relevant. An unverifiable name-drop "
    "is worse than none.\n"
    "- A shared investor (Tier 2) is a weaker tie than a colleague (Tier 1). "
    "Mention it only if it's genuinely apt."
)


def format_intros_for_outreach(intros: list[WarmIntro], *, limit: int = 4) -> Optional[str]:
    """Render a warm-intro block for the outreach user prompt, or None.

    Returns None when there are no intros so the caller can omit the section
    entirely (and keep the prompt cache-friendly).
    """
    if not intros:
        return None

    tier1 = [wi for wi in intros if wi.tier is IntroTier.AT_COMPANY][:limit]
    tier2 = [wi for wi in intros if wi.tier is IntroTier.AT_INVESTOR][:limit]

    lines = [
        "## Warm connections (potential intros, use with care)",
        "",
    ]
    if tier1:
        lines.append("People in the candidate's network who work at this company:")
        for wi in tier1:
            pos = f" — {wi.connection.position}" if wi.connection.position else ""
            lines.append(f"- {wi.connection.full_name}{pos}")
        lines.append("")
    if tier2:
        lines.append("People in the candidate's network who work at this company's investors:")
        for wi in tier2:
            pos = f" — {wi.connection.position}" if wi.connection.position else ""
            lines.append(f"- {wi.connection.full_name}{pos} (at {wi.via})")
        lines.append("")

    lines.append(_OUTREACH_GUIDANCE)
    return "\n".join(lines)
