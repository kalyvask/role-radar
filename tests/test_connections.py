"""Tests for the connections (warm-intro) layer."""

from datetime import date
from pathlib import Path

import pytest

from role_radar.connections.importer import (
    ConnectionsImportError,
    parse_connections_csv,
)
from role_radar.connections.matcher import IntroMatcher
from role_radar.connections.models import Connection, IntroTier
from role_radar.connections.normalize import normalize_company, normalize_vc
from role_radar.connections.service import (
    backers_for,
    format_intros_for_outreach,
)
from role_radar.models import Company, CompanyType
from role_radar.storage import Storage

TODAY = date(2026, 6, 8)


# ---- normalization --------------------------------------------------------


def test_normalize_company_strips_legal_suffix():
    assert normalize_company("OpenAI, Inc.") == "openai"
    assert normalize_company("Anthropic PBC") == "anthropic"
    assert normalize_company("Scale AI") == "scale ai"  # 'ai' is NOT stripped


def test_normalize_company_alias_and_punctuation():
    assert normalize_company("Facebook") == "meta"
    assert normalize_company("  The   Stripe,  Inc. ") == "stripe"


def test_normalize_vc_unifies_firm_suffixes():
    # The crux: an investor recorded as "Sequoia" must match an employer of
    # "Sequoia Capital".
    assert normalize_vc("Sequoia Capital") == normalize_vc("Sequoia") == "sequoia"
    assert normalize_vc("Index Ventures") == "index"
    assert normalize_vc("Greylock Partners") == "greylock"


def test_normalize_vc_aliases():
    assert normalize_vc("a16z") == "andreessen horowitz"
    assert normalize_vc("Andreessen Horowitz") == "andreessen horowitz"
    # Alias wins before suffix-strip would collapse "google ventures" -> "google".
    assert normalize_vc("Google Ventures") == "gv"
    assert normalize_vc("Google") != normalize_vc("Google Ventures")


# ---- importer -------------------------------------------------------------


_LINKEDIN_EXPORT = """Notes:
"When exporting your connection data, you may notice some fields are empty."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Jane,Doe,https://www.linkedin.com/in/janedoe,,Anthropic,Member of Technical Staff,15 Mar 2024
John,Smith,https://www.linkedin.com/in/johnsmith,john@example.com,"OpenAI, Inc.",Product Manager,02 Jan 2023
Aki,Vega,https://www.linkedin.com/in/akivega,,Sequoia Capital,Partner,20 Jun 2022
Jane,Doe,https://www.linkedin.com/in/janedoe,,Anthropic,Member of Technical Staff,15 Mar 2024
,,,,,,
"""


def test_parse_linkedin_export(tmp_path: Path):
    csv_path = tmp_path / "Connections.csv"
    csv_path.write_text(_LINKEDIN_EXPORT, encoding="utf-8")

    conns = parse_connections_csv(csv_path)

    # 3 unique people: the duplicate Jane row and the fully-empty row are dropped.
    assert len(conns) == 3
    names = {c.full_name for c in conns}
    assert names == {"Jane Doe", "John Smith", "Aki Vega"}

    jane = next(c for c in conns if c.full_name == "Jane Doe")
    assert jane.employer == "Anthropic"
    assert jane.employer_norm == "anthropic"
    assert jane.connected_on == date(2024, 3, 15)

    john = next(c for c in conns if c.full_name == "John Smith")
    assert john.employer_norm == "openai"  # legal suffix stripped
    assert john.email == "john@example.com"


def test_parse_handles_header_without_preamble(tmp_path: Path):
    csv_path = tmp_path / "c.csv"
    csv_path.write_text(
        "First Name,Last Name,Company,Position,Connected On\n"
        "Sam,Lee,Cohere,Engineer,10 Feb 2025\n",
        encoding="utf-8",
    )
    conns = parse_connections_csv(csv_path)
    assert len(conns) == 1
    assert conns[0].employer_norm == "cohere"


def test_parse_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConnectionsImportError):
        parse_connections_csv(tmp_path / "nope.csv")


# ---- matcher --------------------------------------------------------------


def _conn(name: str, employer: str, position: str = "", year: int = 2024) -> Connection:
    return Connection(
        full_name=name,
        employer=employer,
        employer_norm=normalize_company(employer),
        position=position,
        connected_on=date(year, 1, 1),
    )


def _matcher() -> IntroMatcher:
    return IntroMatcher(
        [
            _conn("Jane Doe", "Anthropic", "Recruiter"),
            _conn("Bob Roe", "Anthropic", "Software Engineer"),
            _conn("Aki Vega", "Sequoia Capital", "Partner"),
            _conn("Mira Sol", "Some Other Co", "Designer"),
        ],
        today=TODAY,
    )


def test_tier1_matches_company():
    intros = _matcher().intros_for("Anthropic")
    assert {i.connection.full_name for i in intros} == {"Jane Doe", "Bob Roe"}
    assert all(i.tier is IntroTier.AT_COMPANY for i in intros)


def test_tier2_matches_investor():
    intros = _matcher().intros_for("Anthropic", backed_by=["Sequoia"])
    by_tier = {i.tier for i in intros}
    assert IntroTier.AT_INVESTOR in by_tier
    aki = next(i for i in intros if i.connection.full_name == "Aki Vega")
    assert aki.tier is IntroTier.AT_INVESTOR
    assert aki.via == "Sequoia"


def test_tier1_ranks_recruiter_above_engineer():
    # The recruiter at the company should outrank a same-tier engineer.
    intros = _matcher().intros_for("Anthropic")
    assert intros[0].connection.full_name == "Jane Doe"
    assert all(0 <= i.strength <= 100 for i in intros)


def test_no_intros_for_unknown_company():
    assert _matcher().intros_for("Nonexistent Corp") == []


def test_tier2_excludes_megacap_strategic_investors():
    # A connection at Google is NOT a warm path into a Google-backed startup,
    # so Google (a strategic investor) must not generate Tier-2 intros. A real
    # VC in the same backed_by list still does.
    m = IntroMatcher(
        [
            _conn("Googler Person", "Google", "Engineer"),
            _conn("Vc Person", "Sequoia Capital", "Partner"),
        ],
        today=TODAY,
    )
    intros = m.intros_for("SomeStartup", backed_by=["Google", "Sequoia"])
    names = {i.connection.full_name for i in intros}
    assert "Googler Person" not in names
    assert "Vc Person" in names


def test_person_not_double_counted_across_tiers():
    # A connection who is both at the company and (hypothetically) an investor
    # is surfaced once.
    m = IntroMatcher([_conn("Dual Role", "Anthropic", "Partner")], today=TODAY)
    intros = m.intros_for("Anthropic", backed_by=["Anthropic"])
    assert len(intros) == 1
    assert intros[0].tier is IntroTier.AT_COMPANY


# ---- storage round-trip ---------------------------------------------------


def test_storage_replace_and_read(tmp_path: Path):
    storage = Storage(tmp_path / "rr.db")
    rows = [_conn("Jane Doe", "Anthropic", "PM").to_row()]
    written = storage.replace_connections(rows)
    assert written == 1
    assert storage.count_connections() == 1

    got = storage.get_all_connections()
    assert got[0]["full_name"] == "Jane Doe"
    assert got[0]["employer_norm"] == "anthropic"

    # Re-import replaces, not appends.
    storage.replace_connections([_conn("New Person", "OpenAI").to_row()])
    assert storage.count_connections() == 1
    storage.close()


# ---- service --------------------------------------------------------------


def test_backers_for_reads_company_backed_by(tmp_path: Path):
    storage = Storage(tmp_path / "rr.db")
    storage.save_company(
        Company(name="Glean", company_type=CompanyType.VC_BACKED, backed_by=["Sequoia", "Kleiner Perkins"])
    )
    assert backers_for(storage, "Glean") == ["Sequoia", "Kleiner Perkins"]
    assert backers_for(storage, "Unknown") == []
    storage.close()


def test_format_intros_for_outreach_includes_guard():
    intros = _matcher().intros_for("Anthropic", backed_by=["Sequoia"])
    block = format_intros_for_outreach(intros)
    assert block is not None
    assert "Jane Doe" in block
    # The honesty guard must be present.
    assert "suggested I reach out" in block
    assert "Tier 2" not in block or "investor" in block  # tier-2 framed via investor


def test_format_intros_for_outreach_empty_is_none():
    assert format_intros_for_outreach([]) is None
