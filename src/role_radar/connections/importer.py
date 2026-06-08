"""Parse a LinkedIn "Connections.csv" export into `Connection` objects.

LinkedIn's export has a few quirks this handles:
- 2-3 "Notes:" preamble lines before the real header row.
- The header is `First Name,Last Name,URL,Email Address,Company,Position,Connected On`
  (column names have drifted over the years, so we match them loosely).
- Dates look like `15 Mar 2024`.
- Mixed/odd encodings; we decode tolerantly.

The parser is source-agnostic where it can be: any CSV exposing name +
company (+ optional position/url/date) columns will import.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from role_radar.connections.models import Connection
from role_radar.connections.normalize import normalize_company
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)

_DATE_FORMATS = ["%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]

# Column-name candidates (lowercased) -> canonical field.
_COLUMN_MAP = {
    "first name": "first_name",
    "last name": "last_name",
    "url": "linkedin_url",
    "profile url": "linkedin_url",
    "email address": "email",
    "email": "email",
    "company": "employer",
    "current company": "employer",
    "position": "position",
    "title": "position",
    "connected on": "connected_on",
    "connected": "connected_on",
}


class ConnectionsImportError(RuntimeError):
    """Raised when a connections export can't be parsed at all."""


def _decode(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 can't fail, but be explicit.
    return raw.decode("latin-1", errors="replace")


def _find_header_line(lines: list[str]) -> int:
    """Return the index of the real CSV header row, or 0 if not found.

    LinkedIn prepends "Notes:" lines; the header is the first line that
    contains a recognizable name column.
    """
    for i, line in enumerate(lines):
        low = line.lower()
        if "first name" in low and ("company" in low or "position" in low or "url" in low):
            return i
    return 0


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_connections_csv(path: Path) -> list[Connection]:
    """Parse a connections export file into a deduped list of `Connection`.

    Raises:
        ConnectionsImportError: if the file is missing or has no header.
    """
    if not path.exists():
        raise ConnectionsImportError(f"File not found: {path}")

    text = _decode(path)
    lines = text.splitlines()
    if not lines:
        raise ConnectionsImportError(f"File is empty: {path}")

    header_idx = _find_header_line(lines)
    body = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(body))

    if not reader.fieldnames:
        raise ConnectionsImportError(f"No CSV header found in {path}")

    # Build a {actual_column -> canonical_field} resolver for this file.
    resolved: dict[str, str] = {}
    for col in reader.fieldnames:
        canonical = _COLUMN_MAP.get((col or "").strip().lower())
        if canonical:
            resolved[col] = canonical

    if "first_name" not in resolved.values() and "employer" not in resolved.values():
        raise ConnectionsImportError(
            f"{path} doesn't look like a connections export "
            "(no name/company columns found)."
        )

    seen: set[tuple] = set()
    connections: list[Connection] = []
    skipped = 0

    for raw_row in reader:
        fields = {canonical: (raw_row.get(col) or "").strip()
                  for col, canonical in resolved.items()}

        first = fields.get("first_name", "")
        last = fields.get("last_name", "")
        full_name = (f"{first} {last}").strip()
        employer = fields.get("employer", "")

        # A row with neither a name nor an employer is useless for matching.
        if not full_name and not employer:
            skipped += 1
            continue
        if not full_name:
            full_name = employer  # degrade gracefully

        dedupe_key = (
            fields.get("linkedin_url", "")
            or f"{full_name.lower()}|{employer.lower()}"
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        connections.append(
            Connection(
                full_name=full_name,
                first_name=first,
                last_name=last,
                employer=employer,
                employer_norm=normalize_company(employer),
                position=fields.get("position", ""),
                linkedin_url=fields.get("linkedin_url") or None,
                email=fields.get("email") or None,
                connected_on=_parse_date(fields.get("connected_on")),
            )
        )

    logger.info(
        "connections_parsed",
        path=str(path),
        parsed=len(connections),
        skipped=skipped,
    )
    return connections
