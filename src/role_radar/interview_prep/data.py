"""Loaders for the interview-prep JSON snapshots in `data/interview_prep/`.

These mirror the kalyvask/interview-prep repo content (companies, frameworks,
questions, calibrations, laws). They feed into the system prompt as static
context so the LLM matches Alex's actual interview prep system.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


def _data_dir() -> Path:
    """Locate the interview_prep data folder.

    Searches standard install locations in order:
    1. `<cwd>/data/interview_prep` (typical user install)
    2. `<repo_root>/data/interview_prep` (dev install, walking up from this file)
    """
    cwd_candidate = Path.cwd() / "data" / "interview_prep"
    if cwd_candidate.exists():
        return cwd_candidate

    # Walk up from this file: src/role_radar/interview_prep/data.py
    # repo root is 4 parents up
    repo_root = Path(__file__).resolve().parents[3]
    pkg_candidate = repo_root / "data" / "interview_prep"
    if pkg_candidate.exists():
        return pkg_candidate

    raise FileNotFoundError(
        f"interview_prep data directory not found in {cwd_candidate} or {pkg_candidate}"
    )


def _load_json(name: str) -> dict[str, Any]:
    path = _data_dir() / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_companies() -> dict[str, dict[str, Any]]:
    """Return {slug_or_name_lower: company_playbook} for fuzzy lookup."""
    raw = _load_json("companies")
    out: dict[str, dict[str, Any]] = {}
    for c in raw.get("companies", []):
        out[c["slug"]] = c
        out[c["name"].lower()] = c
    return out


@lru_cache(maxsize=1)
def load_frameworks() -> dict[str, Any]:
    return _load_json("frameworks")


@lru_cache(maxsize=1)
def load_questions() -> dict[str, Any]:
    return _load_json("questions")


@lru_cache(maxsize=1)
def load_calibrations() -> dict[str, Any]:
    return _load_json("calibrations")


@lru_cache(maxsize=1)
def load_laws() -> dict[str, Any]:
    return _load_json("laws")


def find_company_playbook(company_name: str) -> Optional[dict[str, Any]]:
    """Look up a company's interview playbook by name (case-insensitive).

    Returns None if no playbook exists for this company.
    """
    if not company_name:
        return None
    companies = load_companies()
    name_lower = company_name.lower().strip()
    if name_lower in companies:
        return companies[name_lower]

    # Try partial match (e.g. "Anthropic Inc" → "anthropic")
    for key, playbook in companies.items():
        if key in name_lower or name_lower in key:
            return playbook
    return None
