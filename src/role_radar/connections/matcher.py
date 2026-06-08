"""Match imported connections to target companies as warm intros.

`IntroMatcher` indexes the network once, then answers `intros_for(company,
backed_by)` cheaply for many companies (the web UI calls it per job).

Two tiers:
- Tier 1 — the connection works at the company itself.
- Tier 2 — the connection works at one of the company's investors.

Ordering uses a transparent 0-100 heuristic. It is NOT a claim about how well
the user actually knows the person — a LinkedIn export only exposes employer,
title, and the date you connected. The score just floats the most useful,
most recent, most senior leads to the top. Every component is documented below
and reflected in the WarmIntro.rationale string.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable, Optional

from role_radar.connections.models import Connection, IntroTier, WarmIntro
from role_radar.connections.normalize import (
    is_strategic_investor,
    normalize_company,
    normalize_vc,
)

# Title keywords that make a connection a stronger lead.
_SENIOR_WORDS = (
    "founder", "co-founder", "cofounder", "ceo", "cto", "cpo", "coo", "cfo",
    "chief", "vp", "vice president", "head of", "director", "partner",
    "principal", "general partner",
)
_MANAGER_WORDS = ("manager", "lead", "staff")
_RECRUITER_WORDS = ("recruit", "talent", "people", "hr ", "human resources", "sourcer")
_PRODUCT_WORDS = ("product manager", "product management", " pm", "product lead", "head of product")

# Tier base scores. Someone *at the company* is the most direct referral path
# for a specific job, so Tier 1 outranks Tier 2 here even though an investor
# bridge is high-leverage in general.
_TIER_BASE = {IntroTier.AT_COMPANY: 60.0, IntroTier.AT_INVESTOR: 40.0}


def _title_has(position: str, words: Iterable[str]) -> bool:
    p = f" {position.lower()} "
    return any(w in p for w in words)


def _recency_bonus(connected_on: Optional[date], today: date) -> tuple[float, str]:
    """Bonus for how recently the connection was made (a weak proxy)."""
    if connected_on is None:
        return 0.0, ""
    months = (today.year - connected_on.year) * 12 + (today.month - connected_on.month)
    if months <= 18:
        return 10.0, f"connected {connected_on.year}"
    if months <= 48:
        return 5.0, f"connected {connected_on.year}"
    return 0.0, f"connected {connected_on.year}"


def _score(conn: Connection, tier: IntroTier, today: date) -> tuple[float, list[str]]:
    """Return (0-100 strength, rationale fragments)."""
    score = _TIER_BASE[tier]
    notes: list[str] = []

    if _title_has(conn.position, _SENIOR_WORDS):
        score += 15
        notes.append("senior")
    elif _title_has(conn.position, _MANAGER_WORDS):
        score += 8

    if tier is IntroTier.AT_COMPANY and _title_has(conn.position, _RECRUITER_WORDS):
        score += 10
        notes.append("recruiting/talent")
    elif _title_has(conn.position, _PRODUCT_WORDS):
        score += 6
        notes.append("product")

    bonus, recency_note = _recency_bonus(conn.connected_on, today)
    score += bonus
    if recency_note:
        notes.append(recency_note)

    return max(0.0, min(100.0, score)), notes


def _rationale(conn: Connection, tier: IntroTier, via: Optional[str], notes: list[str]) -> str:
    where = conn.employer or (via or "")
    if tier is IntroTier.AT_COMPANY:
        head = f"{conn.position or 'Connection'} at {where}".strip()
    else:
        head = f"{conn.position or 'Connection'} at investor {via}".strip()
    if notes:
        head += f" ({', '.join(notes)})"
    return head


class IntroMatcher:
    """Indexes a network and resolves warm intros per company."""

    def __init__(self, connections: list[Connection], *, today: Optional[date] = None):
        self.connections = connections
        # date.today() is avoided in some sandboxes; allow injection for tests.
        self._today = today or date.today()
        self._by_company: dict[str, list[Connection]] = defaultdict(list)
        self._by_vc: dict[str, list[Connection]] = defaultdict(list)
        for c in connections:
            if c.employer_norm:
                self._by_company[c.employer_norm].append(c)
            vc_key = normalize_vc(c.employer)
            if vc_key:
                self._by_vc[vc_key].append(c)

    def intros_for(
        self,
        company_name: str,
        backed_by: Iterable[str] = (),
    ) -> list[WarmIntro]:
        """Return warm intros for one company, strongest first.

        Tier 1 = people at `company_name`. Tier 2 = people at any investor in
        `backed_by`. A person who matches both tiers is kept once, as Tier 1.
        """
        intros: list[WarmIntro] = []
        claimed: set[int] = set()

        # Tier 1: at the company.
        target = normalize_company(company_name)
        if target:
            for conn in self._by_company.get(target, []):
                strength, notes = _score(conn, IntroTier.AT_COMPANY, self._today)
                intros.append(WarmIntro(
                    connection=conn,
                    tier=IntroTier.AT_COMPANY,
                    strength=strength,
                    rationale=_rationale(conn, IntroTier.AT_COMPANY, None, notes),
                ))
                claimed.add(id(conn))

        # Tier 2: at an investor. Skip mega-cap strategic investors (Google,
        # Amazon, ...) — they back startups but employ too many people for
        # "you know someone there" to be a real bridge into the portfolio co.
        for investor in backed_by:
            if is_strategic_investor(investor):
                continue
            inv_key = normalize_vc(investor)
            if not inv_key:
                continue
            for conn in self._by_vc.get(inv_key, []):
                if id(conn) in claimed:
                    continue
                claimed.add(id(conn))
                strength, notes = _score(conn, IntroTier.AT_INVESTOR, self._today)
                intros.append(WarmIntro(
                    connection=conn,
                    tier=IntroTier.AT_INVESTOR,
                    via=investor,
                    strength=strength,
                    rationale=_rationale(conn, IntroTier.AT_INVESTOR, investor, notes),
                ))

        intros.sort(key=lambda wi: (wi.tier is IntroTier.AT_INVESTOR, -wi.strength,
                                    wi.connection.full_name.lower()))
        return intros
