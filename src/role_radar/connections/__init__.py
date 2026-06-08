"""Warm-intro layer: match the user's imported network to target companies.

Public surface:
    Connection, WarmIntro, IntroTier        -- schemas
    parse_connections_csv                   -- import a LinkedIn export
    IntroMatcher                            -- match connections -> companies
    build_matcher, backers_for,
    format_intros_for_outreach              -- storage-backed helpers
"""

from role_radar.connections.importer import (
    ConnectionsImportError,
    parse_connections_csv,
)
from role_radar.connections.matcher import IntroMatcher
from role_radar.connections.models import Connection, IntroTier, WarmIntro
from role_radar.connections.service import (
    backers_for,
    build_matcher,
    format_intros_for_outreach,
    load_connections,
)

__all__ = [
    "Connection",
    "WarmIntro",
    "IntroTier",
    "IntroMatcher",
    "parse_connections_csv",
    "ConnectionsImportError",
    "load_connections",
    "build_matcher",
    "backers_for",
    "format_intros_for_outreach",
]
