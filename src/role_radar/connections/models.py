"""Schemas for the connections (warm-intro) layer.

A `Connection` is one person from the user's imported network. A `WarmIntro`
pairs a connection with a target company and explains why it's a lead: either
the person works at the company (Tier 1) or at one of its investors (Tier 2).
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class IntroTier(str, Enum):
    """How a connection bridges to a target company."""

    AT_COMPANY = "at_company"      # Tier 1: works at the company itself
    AT_INVESTOR = "at_investor"    # Tier 2: works at one of the company's investors

    @property
    def label(self) -> str:
        return "At company" if self is IntroTier.AT_COMPANY else "At investor"


class Connection(BaseModel):
    """One person in the user's imported professional network."""

    full_name: str
    first_name: str = ""
    last_name: str = ""
    employer: str = ""             # raw employer string from the export
    employer_norm: str = ""        # normalized employer, for matching
    position: str = ""             # job title
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    connected_on: Optional[date] = None

    def to_row(self) -> dict:
        """Flatten to the dict shape `Storage.replace_connections` expects."""
        return {
            "full_name": self.full_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "employer": self.employer,
            "employer_norm": self.employer_norm,
            "position": self.position,
            "linkedin_url": self.linkedin_url,
            "email": self.email,
            "connected_on": self.connected_on.isoformat() if self.connected_on else None,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Connection":
        """Rehydrate from a stored connections row."""
        connected_on = None
        raw = row.get("connected_on")
        if raw:
            try:
                connected_on = date.fromisoformat(raw)
            except (ValueError, TypeError):
                connected_on = None
        return cls(
            full_name=row.get("full_name", ""),
            first_name=row.get("first_name") or "",
            last_name=row.get("last_name") or "",
            employer=row.get("employer") or "",
            employer_norm=row.get("employer_norm") or "",
            position=row.get("position") or "",
            linkedin_url=row.get("linkedin_url"),
            email=row.get("email"),
            connected_on=connected_on,
        )


class WarmIntro(BaseModel):
    """A connection surfaced as a potential warm intro to a target company."""

    connection: Connection
    tier: IntroTier
    via: Optional[str] = None       # Tier 2 only: the investor firm that bridges
    strength: float = 0.0           # 0-100 heuristic ordering score (see matcher)
    rationale: str = ""             # short human-readable "why this is a lead"

    def to_card_dict(self) -> dict:
        """Compact, JSON-safe shape for the web job-card payload."""
        return {
            "name": self.connection.full_name,
            "position": self.connection.position,
            "employer": self.connection.employer,
            "tier": self.tier.value,
            "tier_label": self.tier.label,
            "via": self.via,
            "url": self.connection.linkedin_url,
            "strength": round(self.strength),
            "rationale": self.rationale,
        }
