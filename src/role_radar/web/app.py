"""
Flask web application for Role Radar job review UI.
Provides a clean interface to view, like/dislike jobs, and learn from feedback.
"""

import json
import smtplib
import sqlite3
import webbrowser
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
import os

from dateutil import parser as date_parser

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

load_dotenv(override=True)  # override=True so .env beats stale empty shell vars

# Initialize Flask app
app = Flask(__name__,
            template_folder=str(Path(__file__).parent / "templates"),
            static_folder=str(Path(__file__).parent / "static"))

# Default paths
DEFAULT_DB_PATH = Path.home() / ".role_radar" / "feedback.db"
DEFAULT_OUTPUTS_DIR = Path.cwd() / "outputs"


def get_db_path() -> Path:
    """Get the feedback database path."""
    return Path(app.config.get("DB_PATH", DEFAULT_DB_PATH))


def init_feedback_db():
    """Initialize the feedback database."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Create feedback table with dislike_reason and applied
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT UNIQUE NOT NULL,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            feedback TEXT NOT NULL CHECK(feedback IN ('like', 'dislike', 'neutral')),
            dislike_reason TEXT CHECK(dislike_reason IN ('role', 'company', 'location', 'seniority', 'other', NULL)),
            notes TEXT,
            applied INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Add dislike_reason column if it doesn't exist (for migration)
    try:
        cursor.execute("ALTER TABLE job_feedback ADD COLUMN dislike_reason TEXT CHECK(dislike_reason IN ('role', 'company', 'location', 'seniority', 'other', NULL))")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add applied column if it doesn't exist (for migration)
    try:
        cursor.execute("ALTER TABLE job_feedback ADD COLUMN applied INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Create learned preferences table (aggregated from feedback)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learned_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            preference_type TEXT NOT NULL,
            preference_key TEXT NOT NULL,
            weight_adjustment REAL DEFAULT 0.0,
            sample_count INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(preference_type, preference_key)
        )
    """)

    conn.commit()
    conn.close()


def get_feedback(job_id: str) -> Optional[dict]:
    """Get feedback for a specific job."""
    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT feedback, notes, applied FROM job_feedback WHERE job_id = ?",
        (job_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        return {"feedback": row[0], "notes": row[1], "applied": bool(row[2]) if row[2] is not None else False}
    return None


def save_feedback(job_id: str, company: str, title: str, feedback: str, notes: str = "", dislike_reason: str = None, applied: bool = False):
    """Save or update feedback for a job.

    Args:
        dislike_reason: For dislikes, why the user disliked it:
            - 'role': The role itself (title, responsibilities)
            - 'company': The company (culture, reputation)
            - 'location': Location doesn't work
            - 'seniority': Level is too high/low
            - 'other': Other reason
        applied: Whether the user has applied to this job
    """
    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO job_feedback (job_id, company, title, feedback, dislike_reason, notes, applied, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            feedback = excluded.feedback,
            dislike_reason = excluded.dislike_reason,
            notes = excluded.notes,
            applied = excluded.applied,
            updated_at = excluded.updated_at
    """, (job_id, company, title, feedback, dislike_reason, notes, 1 if applied else 0, datetime.now().isoformat()))

    conn.commit()
    conn.close()

    # Update learned preferences based on feedback, considering the reason
    update_learned_preferences(company, title, feedback, dislike_reason)


def update_learned_preferences(company: str, title: str, feedback: str, dislike_reason: str = None):
    """Update learned preferences based on feedback.

    Key insight: Only learn company preferences when the dislike is specifically about the company.
    Role dislikes should only affect title/keyword preferences, not company preferences.
    """
    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.cursor()

    # Weight adjustment: like = +1, dislike = -1
    weight = 1.0 if feedback == "like" else (-1.0 if feedback == "dislike" else 0.0)

    # Only learn company preference if:
    # - It's a LIKE (user likes the company), OR
    # - It's a DISLIKE specifically because of the COMPANY (not the role)
    should_learn_company = (feedback == "like") or (feedback == "dislike" and dislike_reason == "company")

    if should_learn_company:
        cursor.execute("""
            INSERT INTO learned_preferences (preference_type, preference_key, weight_adjustment, sample_count, updated_at)
            VALUES ('company', ?, ?, 1, ?)
            ON CONFLICT(preference_type, preference_key) DO UPDATE SET
                weight_adjustment = (learned_preferences.weight_adjustment * learned_preferences.sample_count + ?) / (learned_preferences.sample_count + 1),
                sample_count = learned_preferences.sample_count + 1,
                updated_at = ?
        """, (company, weight, datetime.now().isoformat(), weight, datetime.now().isoformat()))

    # Extract and learn title keywords
    # Only learn role preferences if it's a like or if the dislike is about the role
    should_learn_role = (feedback == "like") or (feedback == "dislike" and dislike_reason in ("role", "seniority", None))

    if should_learn_role:
        title_lower = title.lower()
        keywords = []

        # Seniority
        if "senior" in title_lower or "sr." in title_lower:
            keywords.append("seniority:senior")
        elif "staff" in title_lower or "principal" in title_lower:
            keywords.append("seniority:staff")
        elif "lead" in title_lower or "director" in title_lower:
            keywords.append("seniority:lead")
        else:
            keywords.append("seniority:mid")

        # Domain keywords
        for domain in ["ai", "ml", "platform", "data", "growth", "infrastructure", "enterprise", "consumer"]:
            if domain in title_lower:
                keywords.append(f"domain:{domain}")

        # Also extract specific role type keywords
        role_types = [
            ("chief of staff", "role_type:cos"),
            ("strategy", "role_type:strategy"),
            ("operations", "role_type:ops"),
            ("bizops", "role_type:ops"),
            ("tpm", "role_type:tpm"),
            ("technical program", "role_type:tpm"),
        ]
        for pattern, keyword in role_types:
            if pattern in title_lower:
                keywords.append(keyword)

        for keyword in keywords:
            cursor.execute("""
                INSERT INTO learned_preferences (preference_type, preference_key, weight_adjustment, sample_count, updated_at)
                VALUES ('title_keyword', ?, ?, 1, ?)
                ON CONFLICT(preference_type, preference_key) DO UPDATE SET
                    weight_adjustment = (learned_preferences.weight_adjustment * learned_preferences.sample_count + ?) / (learned_preferences.sample_count + 1),
                    sample_count = learned_preferences.sample_count + 1,
                    updated_at = ?
            """, (keyword, weight, datetime.now().isoformat(), weight, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def get_learned_preferences() -> dict:
    """Get all learned preferences."""
    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT preference_type, preference_key, weight_adjustment, sample_count
        FROM learned_preferences
        ORDER BY ABS(weight_adjustment) DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    preferences = {"company": {}, "title_keyword": {}}
    for row in rows:
        ptype, pkey, weight, count = row
        if ptype in preferences:
            preferences[ptype][pkey] = {"weight": weight, "count": count}

    return preferences


def get_all_feedback() -> list:
    """Get all job feedback."""
    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT job_id, company, title, feedback, notes, created_at
        FROM job_feedback
        ORDER BY updated_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "job_id": row[0],
            "company": row[1],
            "title": row[2],
            "feedback": row[3],
            "notes": row[4],
            "created_at": row[5]
        }
        for row in rows
    ]


def load_latest_report() -> Optional[dict]:
    """Load the most recent report JSON file."""
    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))

    if not outputs_dir.exists():
        return None

    json_files = sorted(outputs_dir.glob("report_*.json"), reverse=True)
    if not json_files:
        return None

    with open(json_files[0], encoding="utf-8") as f:
        return json.load(f)


def load_reports_last_month() -> list[dict]:
    """Load and merge all job entries from report JSON files in the last 30 days.

    Returns a deduplicated list of job-entry dicts (same shape as report['jobs']),
    with each entry augmented by a 'report_date' key.
    """
    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    if not outputs_dir.exists():
        return []

    cutoff = datetime.now() - timedelta(days=30)
    seen_ids: set[str] = set()
    merged: list[dict] = []

    # Walk newest → oldest so the freshest occurrence of a job wins dedup
    json_files = sorted(outputs_dir.glob("report_*.json"), reverse=True)
    for jf in json_files:
        # Fast-path: skip files clearly older than 30 days by filename date
        try:
            fname_date = datetime.strptime(jf.stem.split("_", 1)[1][:15], "%Y%m%d_%H%M%S")
            if fname_date < cutoff:
                break  # files are sorted newest-first, so we can stop
        except ValueError:
            pass

        try:
            with open(jf, encoding="utf-8") as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        report_date = report.get("generated_at", "")

        for entry in report.get("jobs", []):
            job = entry.get("job", {})
            job_id = job.get("id", f"{job.get('company','')}_{job.get('title','')}")
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            entry = dict(entry)  # shallow copy so we don't mutate the original
            entry["report_date"] = report_date
            merged.append(entry)

    return merged


def get_contacts_path() -> Path:
    """Get the networking contacts JSON path."""
    # Check project data dir first, then fallback
    candidates = [
        Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR)).parent / "data" / "networking_contacts.json",
        Path.cwd() / "data" / "networking_contacts.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


@app.route("/")
def index():
    """Main page showing job listings."""
    return render_template("index.html")


@app.route("/api/contacts")
def api_contacts():
    """API endpoint to get networking contacts."""
    contacts_path = get_contacts_path()
    if contacts_path.exists():
        with open(contacts_path, encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data.get("contacts", {}))
    return jsonify({})


_SENIOR_TITLE_WORDS = {"senior", "sr.", "sr,", "staff", "principal", "lead"}


def _seniority_bucket(title: str) -> int:
    """Return 0 for PM/junior roles, 1 for Senior/Staff/Principal roles.

    Lower bucket = shown first.
    """
    title_lower = title.lower()
    for word in _SENIOR_TITLE_WORDS:
        if word in title_lower:
            return 1
    return 0


import re as _re_filter

def _word_match(keyword: str, text_lower: str) -> bool:
    """True if `keyword` appears in `text_lower` as a whole word, not
    embedded inside another word.

    Substring matching produces false positives for short keywords
    (e.g. "PM" matches "develoPMent", "cos" matches "COSt"). Word
    boundaries fix this.
    """
    kw = keyword.strip().lower()
    if not kw:
        return False
    # Escape regex specials, then anchor at word boundaries
    pattern = r"\b" + _re_filter.escape(kw) + r"\b"
    return _re_filter.search(pattern, text_lower) is not None


def _passes_prefs_filter(title: str, location_raw: str, prefs: dict) -> bool:
    """Lightweight title + location filter so the UI doesn't drown in
    non-PM roles or non-Bay-Area roles when reading directly from the DB.

    Uses preferences.yaml's `allowed_titles`, `excluded_keywords`, and
    `location`. Word-boundary matching avoids "PM" matching "deve**lopm**ent".

    Title logic: an allowed-title match is the primary gate. The
    excluded_keywords list is intended to filter borderline ambiguous
    roles (e.g. "Engineering Manager"), but it overshoots on titles
    like "Product Manager, Infrastructure" where `infrastructure` is
    the feature area, not the role type. When the title contains an
    unambiguous PM phrase ("product manager", "product mgr",
    "head of product"), we skip the excluded check.
    """
    title_lower = title.lower()
    loc_lower = (location_raw or "").lower()

    # Special case: frontier-lab "Member of Technical Staff" naming convention.
    # When the title contains both "member of technical staff" AND "product"
    # (in any order, any separator), it's a PM-adjacent role at xAI / Fireworks
    # / similar even though "MoTS" isn't itself in allowed_titles.
    is_mots_product = (
        "member of technical staff" in title_lower
        and "product" in title_lower
    )

    # Require at least one allowed-title keyword (the primary gate),
    # unless the MoTS-Product special case applies.
    if not is_mots_product:
        allowed = prefs.get("allowed_titles", [])
        if allowed and not any(_word_match(t, title_lower) for t in allowed):
            return False

    # Apply excluded_keywords only to borderline titles. Strong PM
    # signals (explicit "Product Manager" phrase, "Head of Product",
    # or the MoTS-Product frontier-lab convention) are presumed real
    # PM-track roles even if they mention an area that would otherwise
    # be excluded — e.g. "Member of Technical Staff" trips on "Staff",
    # "Senior PM, Infrastructure" trips on "Infrastructure".
    strong_pm_signals = ("product manager", "product mgr", "head of product")
    is_strong_pm = any(sig in title_lower for sig in strong_pm_signals) or is_mots_product
    if not is_strong_pm:
        excluded = prefs.get("excluded_keywords", [])
        if any(_word_match(kw, title_lower) for kw in excluded):
            return False

    # Bay Area location filter (unless include_remote is True)
    if not prefs.get("include_remote", False):
        bay_keywords = [
            "san francisco", "bay area", "palo alto", "mountain view",
            "menlo park", "sunnyvale", "cupertino", "santa clara", "san jose",
            "redwood city", "oakland", "berkeley", "fremont", "san mateo",
            "south bay", "east bay", "peninsula", "silicon valley", "foster city",
            "burlingame", "millbrae",
        ]
        # Substring is fine for cities (they don't embed in other words).
        # "SF" handled separately as standalone to avoid matching "USF", etc.
        is_bay = any(kw in loc_lower for kw in bay_keywords) or _word_match("sf", loc_lower)
        if not is_bay:
            return False

    return True


def _load_preferences() -> dict:
    """Load preferences.yaml. Looks in (a) cwd, (b) the configured
    OUTPUTS_DIR's parent (role-radar root), (c) the project root inferred
    from this file's location. Falls open with an empty dict if missing.
    """
    try:
        import yaml
        # Walk up from this file to find the role-radar project root
        here = Path(__file__).resolve()
        project_root = here.parent.parent.parent.parent  # web/ -> role_radar/ -> src/ -> role-radar/

        candidates = [
            Path.cwd() / "preferences.yaml",
            Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR)).parent / "preferences.yaml",
            project_root / "preferences.yaml",
        ]
        for prefs_path in candidates:
            if prefs_path.exists():
                with open(prefs_path, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
        return {}
    except Exception:
        return {}


@app.route("/api/jobs")
def api_jobs():
    """API endpoint to get jobs from the last 30 days, no cap, sorted by
    seniority bucket (PM/junior first) then posted_date desc.

    Reads jobs directly from the DB (storage.jobs) and overlays score
    info from report_*.json files where available. The DB has the full
    fresh set from the latest scrape; reports only have the email-list
    top-N which is too narrow for browsing.
    """
    one_month_ago = datetime.now() - timedelta(days=30)
    prefs = _load_preferences()

    # ---- 1. Load report scores keyed by job_id ----
    report_index: dict[str, dict] = {}
    for item in load_reports_last_month():
        job = item.get("job", {})
        jid = job.get("id", f"{job.get('company','')}_{job.get('title','')}")
        report_index[jid] = item

    # ---- 2. Load fresh jobs from the DB ----
    db_jobs_data: list[dict] = []
    try:
        from role_radar.storage import Storage
        from role_radar.config import load_settings
        settings = load_settings()
        storage = Storage(settings.cache_dir / "role_radar.db")
        since = datetime.now() - timedelta(days=30)
        # Get jobs fetched in the last 30 days (recent scrapes); we'll
        # then filter to posted in the last 30 days below.
        all_db_jobs = storage.get_jobs(since=since)

        for j in all_db_jobs:
            # Skip if posted_date is missing OR older than 30 days
            if j.posted_date is None:
                continue
            posted_dt = j.posted_date.replace(tzinfo=None) if j.posted_date.tzinfo else j.posted_date
            if posted_dt < one_month_ago:
                continue

            # Apply preferences filter (title + location)
            if not _passes_prefs_filter(j.title, j.location.raw_location, prefs):
                continue

            # Build the job-entry dict the rest of the function expects
            db_jobs_data.append({
                "id": j.id,
                "external_id": j.external_id,
                "company": j.company,
                "company_slug": j.company_slug,
                "title": j.title,
                "apply_url": j.apply_url,
                "posted_date": j.posted_date.isoformat() if j.posted_date else None,
                "description": j.description,
                "location": {
                    "raw_location": j.location.raw_location,
                    "formatted": j.location.raw_location or "Unknown",
                },
                "salary": None,
            })
    except Exception as e:
        # If DB is unavailable, fall back to report-only mode
        import traceback
        app.logger.warning(f"db_jobs_load_failed: {e}\n{traceback.format_exc()}")

    # ---- 3. Merge: report scores overlay on DB jobs; report-only jobs kept too ----
    seen: set[str] = set()
    merged_entries: list[dict] = []

    # First emit DB jobs (with report scores overlaid where matched)
    for entry in db_jobs_data:
        jid = entry["id"]
        seen.add(jid)
        report_item = report_index.get(jid)
        merged_entries.append({
            "job": entry,
            "rank": report_item.get("rank") if report_item else None,
            "score": report_item.get("score") if report_item else None,
            "score_breakdown": report_item.get("score_breakdown", {}) if report_item else {},
            "match_reasons": report_item.get("match_reasons", []) if report_item else [],
            "report_date": report_item.get("report_date") if report_item else None,
        })

    # Then emit report-only jobs (e.g., older scrapes still within 30 days),
    # but apply the same prefs filter so the queue stays focused.
    for jid, item in report_index.items():
        if jid in seen:
            continue
        job = item.get("job", {})
        title = job.get("title", "")
        loc = job.get("location", {})
        loc_raw = loc.get("raw_location", "") if isinstance(loc, dict) else str(loc or "")
        if not _passes_prefs_filter(title, loc_raw, prefs):
            continue
        merged_entries.append(item)

    # ---- 4. Shape into the response the frontend expects ----
    jobs = []
    for item in merged_entries:
        job = item.get("job", {})
        job_id = job.get("id", f"{job.get('company','')}_{job.get('title','')}")

        posted_date_str = job.get("posted_date", job.get("posted_at"))
        posted_dt = None
        if posted_date_str:
            try:
                posted_dt = date_parser.parse(posted_date_str)
                if posted_dt.tzinfo:
                    posted_dt = posted_dt.replace(tzinfo=None)
                if posted_dt < one_month_ago:
                    continue
            except (ValueError, TypeError):
                pass

        feedback_data = get_feedback(job_id)

        location = job.get("location", {})
        if isinstance(location, dict):
            location_str = location.get("formatted", location.get("raw_location", "Unknown"))
        else:
            location_str = str(location) if location else "Unknown"

        salary_data = job.get("salary", {})
        salary_formatted = salary_data.get("formatted", "Not specified") if salary_data else "Not specified"
        salary_is_estimated = salary_data.get("is_estimated", False) if salary_data else False

        title = job.get("title", "")
        jobs.append({
            "id": job_id,
            "rank": item.get("rank"),
            "score": item.get("score"),
            "company": job.get("company"),
            "title": title,
            "location": location_str,
            "salary": salary_formatted,
            "salary_is_estimated": salary_is_estimated,
            "url": job.get("apply_url"),
            "posted_at": posted_date_str,
            "_posted_dt": posted_dt,
            "_seniority_bucket": _seniority_bucket(title),
            "description": job.get("description"),
            "score_breakdown": item.get("score_breakdown", {}),
            "match_reasons": item.get("match_reasons", []),
            "report_date": item.get("report_date"),
            "feedback": feedback_data.get("feedback") if feedback_data else None,
            "notes": feedback_data.get("notes") if feedback_data else None,
            "applied": feedback_data.get("applied", False) if feedback_data else False,
        })

    jobs.sort(key=lambda j: (
        j["_seniority_bucket"],
        -(j["_posted_dt"].timestamp() if j["_posted_dt"] else 0),
    ))

    for j in jobs:
        j.pop("_posted_dt", None)
        j.pop("_seniority_bucket", None)

    return jsonify({
        "jobs": jobs,
        "total": len(jobs),
        "report_date": jobs[0]["report_date"] if jobs and jobs[0].get("report_date") else None,
        "cv_summary": {},
    })


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """API endpoint to save job feedback."""
    data = request.json

    job_id = data.get("job_id")
    company = data.get("company", "")
    title = data.get("title", "")
    feedback = data.get("feedback")
    notes = data.get("notes", "")
    dislike_reason = data.get("dislike_reason")  # role, company, location, seniority, other
    applied = data.get("applied", False)  # Whether user has applied

    if not job_id or feedback not in ("like", "dislike", "neutral"):
        return jsonify({"error": "Invalid input"}), 400

    # Validate dislike_reason if provided
    valid_reasons = ("role", "company", "location", "seniority", "other", None)
    if dislike_reason not in valid_reasons:
        dislike_reason = None

    save_feedback(job_id, company, title, feedback, notes, dislike_reason, applied)

    return jsonify({"success": True})


@app.route("/api/applied", methods=["POST"])
def api_applied():
    """API endpoint to toggle applied status for a job."""
    data = request.json

    job_id = data.get("job_id")
    company = data.get("company", "")
    title = data.get("title", "")
    applied = data.get("applied", False)

    if not job_id:
        return jsonify({"error": "Invalid input"}), 400

    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.cursor()

    # Check if feedback exists
    cursor.execute("SELECT feedback, notes FROM job_feedback WHERE job_id = ?", (job_id,))
    row = cursor.fetchone()

    if row:
        # Update existing record
        cursor.execute("""
            UPDATE job_feedback
            SET applied = ?, updated_at = ?
            WHERE job_id = ?
        """, (1 if applied else 0, datetime.now().isoformat(), job_id))
    else:
        # Create new record with neutral feedback
        cursor.execute("""
            INSERT INTO job_feedback (job_id, company, title, feedback, applied, updated_at)
            VALUES (?, ?, ?, 'neutral', ?, ?)
        """, (job_id, company, title, 1 if applied else 0, datetime.now().isoformat()))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "applied": applied})


@app.route("/api/notes", methods=["POST"])
def api_notes():
    """API endpoint to save notes for a job."""
    data = request.json

    job_id = data.get("job_id")
    company = data.get("company", "")
    title = data.get("title", "")
    notes = data.get("notes", "")

    if not job_id:
        return jsonify({"error": "Invalid input"}), 400

    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.cursor()

    # Check if feedback exists
    cursor.execute("SELECT feedback FROM job_feedback WHERE job_id = ?", (job_id,))
    row = cursor.fetchone()

    if row:
        # Update existing record
        cursor.execute("""
            UPDATE job_feedback
            SET notes = ?, updated_at = ?
            WHERE job_id = ?
        """, (notes, datetime.now().isoformat(), job_id))
    else:
        # Create new record with neutral feedback
        cursor.execute("""
            INSERT INTO job_feedback (job_id, company, title, feedback, notes, updated_at)
            VALUES (?, ?, ?, 'neutral', ?, ?)
        """, (job_id, company, title, notes, datetime.now().isoformat()))

    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route("/api/preferences")
def api_preferences():
    """API endpoint to get learned preferences."""
    return jsonify(get_learned_preferences())


@app.route("/api/feedback/all")
def api_all_feedback():
    """API endpoint to get all feedback history."""
    return jsonify(get_all_feedback())


@app.route("/api/feedback/export")
def api_export_feedback():
    """Export feedback as preferences YAML format."""
    preferences = get_learned_preferences()
    feedback = get_all_feedback()

    # Generate preferences YAML additions
    yaml_lines = ["# Learned preferences from feedback", "# Add these to your preferences.yaml", ""]

    # Company preferences
    liked_companies = [k for k, v in preferences.get("company", {}).items() if v["weight"] > 0]
    disliked_companies = [k for k, v in preferences.get("company", {}).items() if v["weight"] < 0]

    if liked_companies:
        yaml_lines.append("# Preferred companies (from likes)")
        yaml_lines.append("preferred_companies:")
        for company in liked_companies:
            yaml_lines.append(f"  - {company}")
        yaml_lines.append("")

    if disliked_companies:
        yaml_lines.append("# Companies to deprioritize (from dislikes)")
        yaml_lines.append("excluded_companies:")
        for company in disliked_companies:
            yaml_lines.append(f"  - {company}")
        yaml_lines.append("")

    # Title keyword preferences
    keyword_prefs = preferences.get("title_keyword", {})
    liked_keywords = [k.split(":")[1] for k, v in keyword_prefs.items() if v["weight"] > 0 and k.startswith("domain:")]
    disliked_keywords = [k.split(":")[1] for k, v in keyword_prefs.items() if v["weight"] < 0 and k.startswith("domain:")]

    if liked_keywords:
        yaml_lines.append("# Preferred domains (from likes)")
        yaml_lines.append("preferred_domains:")
        for kw in liked_keywords:
            yaml_lines.append(f"  - {kw}")
        yaml_lines.append("")

    return jsonify({
        "yaml": "\n".join(yaml_lines),
        "summary": {
            "total_feedback": len(feedback),
            "likes": len([f for f in feedback if f["feedback"] == "like"]),
            "dislikes": len([f for f in feedback if f["feedback"] == "dislike"]),
            "learned_company_preferences": len(preferences.get("company", {})),
            "learned_keyword_preferences": len(preferences.get("title_keyword", {}))
        }
    })


def send_email_via_resend(to_email: str, jobs: list) -> dict:
    """Send email using Resend API (free, no credentials needed from user)."""
    try:
        import resend

        # Using Resend's test/demo mode - sends from onboarding@resend.dev
        resend.api_key = "re_123456789"  # Placeholder - will use test mode

        html_content = generate_email_html(jobs)

        # For now, save locally and provide instructions
        # In production, you'd use a real Resend API key
        return {
            "error": "Resend API requires signup. Using alternative method...",
            "fallback": True
        }
    except ImportError:
        return {"error": "Resend not installed", "fallback": True}
    except Exception as e:
        return {"error": str(e), "fallback": True}


def send_email_report(to_email: str, jobs: list) -> dict:
    """Send the job report via email."""
    # Generate HTML email content
    html_content = generate_email_html(jobs)
    plain_content = generate_email_plain(jobs)

    # First try Resend (works without user credentials)
    resend_result = send_email_via_resend(to_email, jobs)
    if resend_result.get("success"):
        return resend_result

    # Fall back to SMTP if configured
    smtp_host = os.getenv("ROLE_RADAR_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("ROLE_RADAR_SMTP_PORT", "587"))
    smtp_username = os.getenv("ROLE_RADAR_SMTP_USERNAME", "")
    smtp_password = os.getenv("ROLE_RADAR_SMTP_PASSWORD", "")
    from_email = os.getenv("ROLE_RADAR_EMAIL_FROM", smtp_username)
    test_mode = os.getenv("ROLE_RADAR_EMAIL_TEST_MODE", "true").lower() == "true"

    # If SMTP not configured or test mode, save to file and open in browser
    if not smtp_password or smtp_password == "your-app-password-here" or test_mode:
        # Save HTML to temp file and open in browser
        import tempfile
        import webbrowser

        # Save to outputs directory
        outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
        outputs_dir.mkdir(parents=True, exist_ok=True)

        email_file = outputs_dir / f"email_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        email_file.write_text(html_content, encoding="utf-8")

        # Open in default browser
        webbrowser.open(f"file://{email_file.absolute()}")

        return {
            "success": True,
            "message": f"Email opened in browser! ({len(jobs)} jobs)",
            "note": "To receive actual emails, configure Gmail App Password in .env",
            "file": str(email_file)
        }

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Role Radar: {len(jobs)} new PM roles for you"
        msg["From"] = from_email
        msg["To"] = to_email

        msg.attach(MIMEText(plain_content, "plain"))
        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.sendmail(from_email, to_email, msg.as_string())

        return {"success": True, "message": f"Sent {len(jobs)} jobs to {to_email}"}

    except Exception as e:
        return {"error": str(e)}


def generate_email_html(jobs: list) -> str:
    """Generate HTML email content."""
    jobs_html = ""
    for job in jobs[:30]:  # Limit to 30 jobs
        jobs_html += f"""
        <tr style="border-bottom: 1px solid #2d3a4f;">
            <td style="padding: 16px;">
                <div style="font-weight: bold; color: #da7756; font-size: 16px;">#{job.get('rank', '-')}</div>
            </td>
            <td style="padding: 16px;">
                <div style="font-weight: bold; font-size: 18px; color: #e8e8e8; margin-bottom: 4px;">
                    {job.get('company', 'Unknown')}
                </div>
                <div style="font-size: 16px; color: #da7756; margin-bottom: 4px;">
                    <a href="{job.get('url', '#')}" style="color: #da7756; text-decoration: none;">
                        {job.get('title', 'Unknown')}
                    </a>
                </div>
                <div style="font-size: 14px; color: #9ca3af;">
                    {job.get('location', 'Location not specified')}
                </div>
            </td>
            <td style="padding: 16px; text-align: center;">
                <div style="font-size: 24px; font-weight: bold; color: {'#4ade80' if job.get('score', 0) >= 70 else '#da7756'};">
                    {job.get('score', '-')}
                </div>
            </td>
            <td style="padding: 16px; text-align: center;">
                <a href="{job.get('url', '#')}"
                   style="background: #da7756; color: white; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-weight: bold;">
                    Apply
                </a>
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background: #1a1a2e; }}
        </style>
    </head>
    <body style="background: #1a1a2e; color: #e8e8e8; padding: 20px;">
        <div style="max-width: 800px; margin: 0 auto; background: #16213e; border-radius: 16px; overflow: hidden;">
            <!-- Header -->
            <div style="background: linear-gradient(135deg, #da7756 0%, #f0a080 100%); padding: 30px; text-align: center;">
                <h1 style="margin: 0; color: white; font-size: 28px;">Role Radar</h1>
                <p style="margin: 10px 0 0; color: rgba(255,255,255,0.9); font-size: 16px;">
                    {len(jobs)} PM & Strategy roles matched your profile
                </p>
            </div>

            <!-- Jobs Table -->
            <table style="width: 100%; border-collapse: collapse;">
                <thead>
                    <tr style="background: #1a1a2e; color: #9ca3af; font-size: 14px;">
                        <th style="padding: 12px 16px; text-align: left; width: 50px;">Rank</th>
                        <th style="padding: 12px 16px; text-align: left;">Job Details</th>
                        <th style="padding: 12px 16px; text-align: center; width: 80px;">Score</th>
                        <th style="padding: 12px 16px; text-align: center; width: 100px;">Action</th>
                    </tr>
                </thead>
                <tbody>
                    {jobs_html}
                </tbody>
            </table>

            <!-- Footer -->
            <div style="padding: 20px; text-align: center; color: #9ca3af; font-size: 14px; border-top: 1px solid #2d3a4f;">
                <p>Generated by Role Radar on {datetime.now().strftime('%B %d, %Y')}</p>
                <p>View all jobs and manage preferences at <a href="http://localhost:5000" style="color: #da7756;">localhost:5000</a></p>
            </div>
        </div>
    </body>
    </html>
    """


def generate_email_plain(jobs: list) -> str:
    """Generate plain text email content."""
    lines = [
        "ROLE RADAR - Job Report",
        f"{len(jobs)} PM & Strategy roles matched your profile",
        "=" * 50,
        ""
    ]

    for job in jobs[:30]:
        lines.extend([
            f"#{job.get('rank', '-')} | Score: {job.get('score', '-')}",
            f"Company: {job.get('company', 'Unknown')}",
            f"Title: {job.get('title', 'Unknown')}",
            f"Location: {job.get('location', 'Not specified')}",
            f"Apply: {job.get('url', 'N/A')}",
            "-" * 30,
            ""
        ])

    lines.extend([
        f"Generated on {datetime.now().strftime('%B %d, %Y')}",
        "View all jobs at http://localhost:5000"
    ])

    return "\n".join(lines)


@app.route("/prep-files/<path:filename>")
def serve_prep_file(filename: str):
    """Serve a generated prep doc (Markdown or DOCX) from outputs/prep/.

    Used so the UI can offer one-click open/download of just-generated docs.
    Path-traversal-safe via Flask's send_from_directory.
    """
    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    prep_dir = outputs_dir / "prep"
    if not prep_dir.exists():
        return "prep directory not found", 404
    return send_from_directory(prep_dir, filename, as_attachment=False)


@app.route("/prep-view/<path:filename>")
def view_prep_doc(filename: str):
    """Render a prep doc Markdown file as styled HTML for in-browser reading.

    Mirrors the visual feel of the Snowflake reference prep doc — wide-margin
    serif typography, numbered sections, hairline rules, generous spacing.
    """
    import markdown as _markdown

    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    md_path = outputs_dir / "prep" / filename
    if not md_path.exists() or not md_path.is_file() or md_path.suffix != ".md":
        return "prep markdown not found", 404

    md_text = md_path.read_text(encoding="utf-8")
    html_body = _markdown.markdown(
        md_text,
        extensions=["extra", "sane_lists", "smarty", "toc"],
        output_format="html5",
    )

    # Pull the title (first H1) for the page <title>
    page_title = filename
    for line in md_text.splitlines():
        if line.startswith("# "):
            page_title = line[2:].strip()
            break

    docx_name = md_path.with_suffix(".docx").name

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:        #F7F6F3;
    --surface:   #FFFFFF;
    --rule:      #EAE7E0;
    --rule-soft: #F0EDE6;
    --ink:       #1F1D1A;
    --ink-muted: #6E6A63;
    --ink-soft:  #9A958C;
    --accent:    #C2410C;
    --accent-bg: #FBEBE0;
    --good:      #15803D;
  }}
  * {{ box-sizing: border-box; }}
  html {{ background: var(--bg); }}
  body {{
    margin: 0;
    padding: 64px 24px 96px;
    color: var(--ink);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
    font-size: 16.5px;
    line-height: 1.65;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  .toolbar {{
    position: fixed; top: 16px; right: 24px;
    display: flex; gap: 12px; align-items: center;
    background: var(--surface); border: 1px solid var(--rule);
    padding: 8px 14px; border-radius: 999px;
    font-size: 13px;
    box-shadow: 0 1px 2px rgba(0,0,0,.03);
  }}
  .toolbar a {{ color: var(--accent); text-decoration: none; font-weight: 500; }}
  .toolbar a:hover {{ color: var(--ink); }}
  .toolbar .sep {{ color: var(--rule); }}
  h1, h2, h3, h4 {{
    font-family: 'Fraunces', 'Iowan Old Style', Georgia, serif;
    font-weight: 500;
    letter-spacing: -0.01em;
    color: var(--ink);
  }}
  h1 {{ font-size: 42px; line-height: 1.1; margin: 0 0 12px; }}
  h2 {{
    font-size: 28px; line-height: 1.2;
    margin: 56px 0 16px;
    padding-top: 28px;
    border-top: 1px solid var(--rule);
  }}
  h3 {{ font-size: 21px; margin: 32px 0 10px; }}
  h4 {{ font-size: 17px; margin: 24px 0 6px; }}
  p {{ margin: 0 0 16px; }}
  p strong, li strong {{ color: var(--ink); font-weight: 600; }}
  em {{ color: var(--ink-muted); }}
  ul, ol {{ padding-left: 22px; margin: 0 0 16px; }}
  li {{ margin: 6px 0; }}
  hr {{ border: none; border-top: 1px solid var(--rule); margin: 40px 0; }}
  blockquote {{
    margin: 16px 0;
    padding: 12px 18px;
    border-left: 3px solid var(--accent);
    background: var(--accent-bg);
    color: var(--ink);
    font-style: italic;
  }}
  blockquote p {{ margin: 0; }}
  code {{
    font-family: 'JetBrains Mono', Consolas, 'Liberation Mono', monospace;
    font-size: 0.92em;
    background: var(--rule-soft);
    padding: 2px 6px;
    border-radius: 3px;
  }}
  pre {{ background: #1F1D1A; color: #F7F6F3; padding: 16px; border-radius: 4px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; color: inherit; }}
  /* Center the header block (Title + meta) */
  .wrap > h1:first-child {{ text-align: center; }}
  .wrap > h1:first-child + p {{ text-align: center; color: var(--ink-muted); margin-bottom: 8px; }}
  .wrap > h1:first-child + p + p {{ text-align: center; color: var(--ink-muted); }}
  .wrap > h1:first-child + p + p + p em {{ display: block; text-align: center; margin-top: 4px; }}
  .wrap > hr:first-of-type {{ margin-top: 32px; }}
  @media (max-width: 640px) {{
    body {{ padding: 32px 16px 64px; }}
    h1 {{ font-size: 32px; }}
    h2 {{ font-size: 24px; }}
    .toolbar {{ position: static; margin-bottom: 24px; }}
  }}
  @media print {{
    .toolbar {{ display: none; }}
    body {{ background: white; padding: 0; }}
  }}
</style>
</head>
<body>
<div class="toolbar">
  <a href="/prep-files/{docx_name}" download>⬇ Download DOCX</a>
  <span class="sep">·</span>
  <a href="/prep-files/{filename}" target="_blank">View source</a>
  <span class="sep">·</span>
  <a href="/" target="_self">← Back to jobs</a>
</div>
<div class="wrap">
{html_body}
</div>
</body>
</html>"""


@app.route("/api/prep/stream")
def api_prep_stream():
    """SSE endpoint that streams phase + elapsed-time progress events as a prep
    doc generates.

    Query params:
        job_id: required.
        review: 'true' / 'false', defaults to 'true'.

    Events emitted (each as JSON in `data: ...`):
        {"phase": "parsing_cv"}
        {"phase": "generating"}                  # Claude generation call started
        {"phase": "tick", "elapsed": 12}         # heartbeat every 2s
        {"phase": "reviewing"}                   # Claude review call started
        {"phase": "writing_files"}
        {"phase": "done", "markdown_path": ..., "docx_path": ..., "review": {...}}
        {"phase": "error", "error": "..."}
    """
    import json as _json
    import queue as _queue
    import threading as _threading
    import time as _time

    from flask import Response, stream_with_context

    job_id = request.args.get("job_id", "")
    do_review = request.args.get("review", "true").lower() != "false"

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    cv_path_str = os.getenv("ROLE_RADAR_CV_PATH")
    if not cv_path_str:
        return jsonify({"error": "ROLE_RADAR_CV_PATH not set in .env"}), 400
    cv_path = Path(cv_path_str)
    if not cv_path.exists():
        return jsonify({"error": f"CV not found at {cv_path}"}), 400

    # Resolve job up-front so we can fail fast with a 4xx (not in the SSE stream)
    from role_radar.interview_prep.report_loader import (
        find_jobs_in_report,
        find_latest_report,
        load_report,
    )

    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    report_path = find_latest_report(outputs_dir)
    if report_path is None:
        return jsonify({"error": "No report found in outputs directory."}), 404
    report_data = load_report(report_path)
    matches = find_jobs_in_report(report_data, job_id=job_id)
    if not matches:
        return jsonify({"error": f"Job {job_id} not found in {report_path.name}"}), 404
    _, job = matches[0]

    @stream_with_context
    def event_stream():
        from role_radar.cv_parser import parse_cv
        from role_radar.interview_prep import (
            generate_prep_for_job,
            render_review_markdown,
            review_prep_doc,
        )

        events: _queue.Queue = _queue.Queue()

        def emit(payload: dict) -> None:
            events.put(payload)

        def worker() -> None:
            try:
                emit({"phase": "parsing_cv"})
                cv_signals = parse_cv(cv_path)

                emit({"phase": "generating"})
                candidate_name = os.getenv("ROLE_RADAR_CANDIDATE_NAME", "Candidate")
                prep_doc, md_path, docx_path = generate_prep_for_job(
                    job=job,
                    cv=cv_signals,
                    candidate_name=candidate_name,
                    output_dir=outputs_dir / "prep",
                    cv_excerpt=cv_signals.raw_text,
                    write_docx=True,
                )

                review_summary = None
                if do_review:
                    emit({"phase": "reviewing"})
                    try:
                        report = review_prep_doc(md_path.read_text(encoding="utf-8"))
                        review_md = render_review_markdown(report)
                        with open(md_path, "a", encoding="utf-8") as f:
                            f.write(review_md)
                        review_summary = {
                            "score": report.overall_score,
                            "findings": len(report.findings),
                            "recommendation": report.ship_recommendation,
                        }
                    except Exception as e:
                        review_summary = {"error": str(e)}

                emit({"phase": "writing_files"})
                emit({
                    "phase": "done",
                    "company": job.company,
                    "title": job.title,
                    "markdown_path": str(md_path),
                    "docx_path": str(docx_path) if docx_path else None,
                    "review": review_summary,
                })
            except Exception as e:
                emit({"phase": "error", "error": str(e)})

        thread = _threading.Thread(target=worker, daemon=True)
        thread.start()
        start = _time.time()

        while True:
            try:
                event = events.get(timeout=2.0)
                yield f"data: {_json.dumps(event)}\n\n"
                if event.get("phase") in ("done", "error"):
                    break
            except _queue.Empty:
                if not thread.is_alive():
                    # Worker died without emitting terminal event
                    yield f"data: {_json.dumps({'phase': 'error', 'error': 'worker exited unexpectedly'})}\n\n"
                    break
                yield f"data: {_json.dumps({'phase': 'tick', 'elapsed': int(_time.time() - start)})}\n\n"

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
        },
    )


@app.route("/api/prep/generate", methods=["POST"])
def api_generate_prep():
    """Generate an LLM-powered interview prep doc for a job by ID.

    Looks up the job in the latest report, calls Claude to generate the prep,
    and writes Markdown + DOCX to outputs/prep/. Returns the file paths.

    Required env: ANTHROPIC_API_KEY. Required config: cv path via
    ROLE_RADAR_CV_PATH env var (or pass `cv_path` in the request body).
    """
    data = request.json or {}
    job_id = data.get("job_id")
    do_review = bool(data.get("review", False))
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    # Locate CV
    cv_path_str = data.get("cv_path") or os.getenv("ROLE_RADAR_CV_PATH")
    if not cv_path_str:
        return jsonify({
            "error": "No CV configured. Set ROLE_RADAR_CV_PATH in your .env or pass cv_path in the request."
        }), 400
    cv_path = Path(cv_path_str)
    if not cv_path.exists():
        return jsonify({"error": f"CV not found at {cv_path}"}), 400

    # Find the job in the latest report
    from role_radar.cv_parser import parse_cv
    from role_radar.interview_prep import (
        PrepGenerationError,
        generate_prep_for_job,
        render_review_markdown,
        review_prep_doc,
    )
    from role_radar.interview_prep.report_loader import (
        find_jobs_in_report,
        find_latest_report,
        load_report,
    )

    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    report_path = find_latest_report(outputs_dir)
    if report_path is None:
        return jsonify({"error": "No report found in outputs directory."}), 404

    report_data = load_report(report_path)
    matches = find_jobs_in_report(report_data, job_id=job_id)
    if not matches:
        return jsonify({"error": f"Job {job_id} not found in {report_path.name}"}), 404

    _, job = matches[0]

    try:
        cv_signals = parse_cv(cv_path)
        candidate_name = os.getenv("ROLE_RADAR_CANDIDATE_NAME", "Candidate")
        prep_doc, md_path, docx_path = generate_prep_for_job(
            job=job,
            cv=cv_signals,
            candidate_name=candidate_name,
            output_dir=outputs_dir / "prep",
            cv_excerpt=cv_signals.raw_text,
            write_docx=True,
        )
    except PrepGenerationError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    review_summary = None
    if do_review:
        try:
            report = review_prep_doc(md_path.read_text(encoding="utf-8"))
            review_md = render_review_markdown(report)
            with open(md_path, "a", encoding="utf-8") as f:
                f.write(review_md)
            review_summary = {
                "score": report.overall_score,
                "findings": len(report.findings),
                "recommendation": report.ship_recommendation,
            }
        except Exception as e:
            review_summary = {"error": str(e)}

    return jsonify({
        "success": True,
        "company": job.company,
        "title": job.title,
        "markdown_path": str(md_path),
        "docx_path": str(docx_path) if docx_path else None,
        "review": review_summary,
    })


# ----- Company Review (LLM-powered, web_search-backed) ---------------------


def _resolve_company_hints(company: str) -> dict:
    """Look up homepage / careers / category / funding for a company name.

    Tries the merged AI seed list + YAML overrides. Returns an empty dict if
    the company isn't tracked — the generator will still run, just without hints.
    """
    try:
        from role_radar.company_sources.ai_top20 import (
            AI_COMPANIES_SEED,
            load_overrides,
        )
    except Exception:
        return {}

    overrides_path = Path.cwd() / "data" / "ai_companies.yaml"
    additions, removals = load_overrides(overrides_path) if overrides_path.exists() else ([], set())
    pool = [c for c in AI_COMPANIES_SEED if c.name.lower() not in removals] + additions

    target = company.strip().lower()
    for c in pool:
        if c.name.lower() == target:
            return {
                "homepage": getattr(c, "homepage", None),
                "careers_url": getattr(c, "careers_url", None),
                "category": getattr(c.category, "value", None) if getattr(c, "category", None) else None,
                "funding_amount_m": getattr(c, "funding_amount_m", None),
            }
    return {}


@app.route("/review-files/<path:filename>")
def serve_review_file(filename: str):
    """Serve a generated company review (Markdown) from outputs/reviews/."""
    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    reviews_dir = outputs_dir / "reviews"
    if not reviews_dir.exists():
        return "reviews directory not found", 404
    return send_from_directory(reviews_dir, filename, as_attachment=False)


@app.route("/review-view/<path:filename>")
def view_review_doc(filename: str):
    """Render a company review Markdown file as styled HTML for in-browser reading.

    Uses the same visual treatment as the prep doc viewer (Fraunces serif,
    wide margins, hairline rules) so the two doc types feel like a set.
    """
    import markdown as _markdown

    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    md_path = outputs_dir / "reviews" / filename
    if not md_path.exists() or not md_path.is_file() or md_path.suffix != ".md":
        return "review markdown not found", 404

    md_text = md_path.read_text(encoding="utf-8")

    # Convert ```mermaid blocks into <div class="mermaid"> so mermaid.js can
    # find and render them after the markdown→HTML conversion runs. We do
    # this BEFORE handing the doc to the markdown extension so the fenced
    # block doesn't get wrapped in <pre><code>.
    import re as _re
    md_text_for_render = _re.sub(
        r"```mermaid\n(.*?)```",
        lambda m: f'<div class="mermaid">\n{m.group(1)}\n</div>',
        md_text,
        flags=_re.DOTALL,
    )

    html_body = _markdown.markdown(
        md_text_for_render,
        extensions=["extra", "sane_lists", "smarty", "toc", "tables"],
        output_format="html5",
    )

    page_title = filename
    for line in md_text.splitlines():
        if line.startswith("# "):
            page_title = line[2:].strip()
            break

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
<title>{page_title}</title>
<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
<link href=\"https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Inter:wght@400;500;600&display=swap\" rel=\"stylesheet\">
<script type=\"module\">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{
    startOnLoad: true,
    theme: 'base',
    themeVariables: {{
      fontFamily: 'Inter, sans-serif',
      primaryColor: '#E3F2FD',
      primaryTextColor: '#1F1D1A',
      primaryBorderColor: '#1565C0',
      lineColor: '#1565C0',
      secondaryColor: '#FBEBE0',
      tertiaryColor: '#F7F6F3',
    }},
  }});
</script>
<style>
  :root {{
    --bg: #F7F6F3; --surface: #FFFFFF; --rule: #EAE7E0; --rule-soft: #F0EDE6;
    --ink: #1F1D1A; --ink-muted: #6E6A63; --ink-soft: #9A958C;
    --accent: #1565C0; --accent-bg: #E3F2FD; --good: #15803D;
  }}
  * {{ box-sizing: border-box; }}
  html {{ background: var(--bg); }}
  body {{
    margin: 0; padding: 64px 24px 96px; color: var(--ink);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
    font-size: 16.5px; line-height: 1.65; -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  .toolbar {{
    position: fixed; top: 16px; right: 24px; display: flex; gap: 12px; align-items: center;
    background: var(--surface); border: 1px solid var(--rule);
    padding: 8px 14px; border-radius: 999px; font-size: 13px;
    box-shadow: 0 1px 2px rgba(0,0,0,.03);
  }}
  .toolbar a {{ color: var(--accent); text-decoration: none; font-weight: 500; }}
  .toolbar a:hover {{ color: var(--ink); }}
  .toolbar .sep {{ color: var(--rule); }}
  h1, h2, h3, h4 {{
    font-family: 'Fraunces', 'Iowan Old Style', Georgia, serif;
    font-weight: 500; letter-spacing: -0.01em; color: var(--ink);
  }}
  h1 {{ font-size: 42px; line-height: 1.1; margin: 0 0 12px; }}
  h2 {{
    font-size: 28px; line-height: 1.2; margin: 56px 0 16px;
    padding-top: 28px; border-top: 1px solid var(--rule);
  }}
  h3 {{ font-size: 21px; margin: 32px 0 10px; }}
  h4 {{ font-size: 17px; margin: 24px 0 6px; }}
  p {{ margin: 0 0 16px; }}
  p strong, li strong {{ color: var(--ink); font-weight: 600; }}
  em {{ color: var(--ink-muted); }}
  ul, ol {{ padding-left: 22px; margin: 0 0 16px; }}
  li {{ margin: 6px 0; }}
  hr {{ border: none; border-top: 1px solid var(--rule); margin: 40px 0; }}
  blockquote {{
    margin: 16px 0; padding: 12px 18px;
    border-left: 3px solid var(--accent); background: var(--accent-bg);
    color: var(--ink); font-style: italic;
  }}
  blockquote p {{ margin: 0; font-style: normal; }}
  blockquote strong {{ color: var(--accent); font-weight: 600; }}
  a {{ color: var(--accent); text-decoration: underline; text-decoration-color: var(--rule); text-underline-offset: 3px; }}
  a:hover {{ text-decoration-color: var(--accent); }}
  code {{
    font-family: 'JetBrains Mono', Consolas, monospace; font-size: 0.92em;
    background: var(--rule-soft); padding: 2px 6px; border-radius: 3px;
  }}
  /* Tables: clean, scannable, not heavy. */
  table {{
    border-collapse: collapse;
    margin: 18px 0 22px;
    width: 100%;
    font-size: 14.5px;
    background: var(--surface);
    border: 1px solid var(--rule);
    border-radius: 4px;
    overflow: hidden;
  }}
  thead {{ background: var(--rule-soft); }}
  th, td {{
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid var(--rule);
    vertical-align: top;
  }}
  th {{
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--rule-soft); }}
  td a {{ word-break: break-word; }}
  /* Mermaid charts: centered, with a soft frame. */
  .mermaid {{
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 160px;
    margin: 24px 0 28px;
    padding: 18px;
    background: var(--surface);
    border: 1px solid var(--rule);
    border-radius: 6px;
    overflow-x: auto;
  }}
  .mermaid svg {{ max-width: 100%; height: auto; }}
  @media (max-width: 640px) {{
    body {{ padding: 32px 16px 64px; }}
    h1 {{ font-size: 32px; }} h2 {{ font-size: 24px; }}
    .toolbar {{ position: static; margin-bottom: 24px; }}
    table {{ font-size: 13px; }}
    th, td {{ padding: 8px 10px; }}
  }}
  @media print {{ .toolbar {{ display: none; }} body {{ background: white; padding: 0; }} }}
</style>
</head>
<body>
<div class=\"toolbar\">
  <a href=\"/review-files/{filename}\" target=\"_blank\">View source</a>
  <span class=\"sep\">·</span>
  <a href=\"/\" target=\"_self\">← Back to jobs</a>
</div>
<div class=\"wrap\">
{html_body}
</div>
</body>
</html>"""


@app.route("/api/review/stream")
def api_review_stream():
    """SSE endpoint that streams progress while a company review is generated.

    Query params:
        company: required.

    Events emitted:
        {"phase": "starting"}
        {"phase": "researching"}             # Claude generation call started
        {"phase": "tick", "elapsed": 12}     # heartbeat every 2s
        {"phase": "writing_files"}
        {"phase": "done", "markdown_path": ..., "signal": ..., "tokens": ...}
        {"phase": "error", "error": "..."}
    """
    import json as _json
    import queue as _queue
    import threading as _threading
    import time as _time

    from flask import Response, stream_with_context

    company = (request.args.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400

    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))

    @stream_with_context
    def event_stream():
        from role_radar.company_review import (
            CompanyReviewGenerationError,
            generate_review_for_company,
        )

        events: _queue.Queue = _queue.Queue()

        def emit(payload: dict) -> None:
            events.put(payload)

        def worker() -> None:
            try:
                emit({"phase": "starting"})
                hints = _resolve_company_hints(company)

                emit({"phase": "researching"})
                doc, md_path = generate_review_for_company(
                    company=company,
                    output_dir=outputs_dir / "reviews",
                    homepage=hints.get("homepage"),
                    careers_url=hints.get("careers_url"),
                    category=hints.get("category"),
                    funding_amount_m=hints.get("funding_amount_m"),
                )

                emit({"phase": "writing_files"})
                emit({
                    "phase": "done",
                    "company": company,
                    "markdown_path": str(md_path),
                    "signal": doc.overall_signal,
                    "headline": doc.headline_summary,
                    "input_tokens": doc.input_tokens,
                    "output_tokens": doc.output_tokens,
                    "web_search_count": doc.web_search_count,
                    "duration_seconds": doc.duration_seconds,
                })
            except CompanyReviewGenerationError as e:
                emit({"phase": "error", "error": str(e)})
            except Exception as e:
                emit({"phase": "error", "error": f"Unexpected error: {e}"})

        thread = _threading.Thread(target=worker, daemon=True)
        thread.start()
        start = _time.time()

        while True:
            try:
                event = events.get(timeout=2.0)
                yield f"data: {_json.dumps(event)}\n\n"
                if event.get("phase") in ("done", "error"):
                    break
            except _queue.Empty:
                if not thread.is_alive():
                    yield f"data: {_json.dumps({'phase': 'error', 'error': 'worker exited unexpectedly'})}\n\n"
                    break
                yield f"data: {_json.dumps({'phase': 'tick', 'elapsed': int(_time.time() - start)})}\n\n"

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/review/list")
def api_list_reviews():
    """List all previously generated company reviews so the UI can show
    'already generated' state on each company.
    """
    outputs_dir = Path(app.config.get("OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
    reviews_dir = outputs_dir / "reviews"
    if not reviews_dir.exists():
        return jsonify({"reviews": []})

    reviews = []
    for p in sorted(reviews_dir.glob("*.md"), reverse=True):
        # Filename pattern: {slug}__review_{YYYYMMDD_HHMM}.md
        # The renderer prepends a metadata block (verdict + generated-at) before
        # the H1, so we have to look past those lines for the actual `# ` title.
        display = p.stem
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    display = title.split(" — ")[0] if " — " in title else title
                    break
        except Exception:
            pass
        reviews.append({
            "filename": p.name,
            "company": display,
            "size_bytes": p.stat().st_size,
            "modified_iso": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
        })
    return jsonify({"reviews": reviews})


# ----- Email -----------------------------------------------------------------


@app.route("/api/email/send", methods=["POST"])
def api_send_email():
    """API endpoint to send the job report via email."""
    data = request.json or {}
    to_email = data.get("to_email") or os.getenv("ROLE_RADAR_EMAIL_TO", "")

    if not to_email:
        return jsonify({"error": "No email address provided"}), 400

    # Get jobs from latest report
    report = load_latest_report()
    if not report:
        return jsonify({"error": "No report found"}), 404

    job_items = report.get("jobs", report.get("top_jobs", []))
    jobs = []

    for item in job_items:
        job = item.get("job", {})
        location = job.get("location", {})
        if isinstance(location, dict):
            location_str = location.get("formatted", location.get("raw_location", "Unknown"))
        else:
            location_str = str(location) if location else "Unknown"

        jobs.append({
            "rank": item.get("rank"),
            "score": item.get("score"),
            "company": job.get("company"),
            "title": job.get("title"),
            "location": location_str,
            "url": job.get("apply_url")
        })

    result = send_email_report(to_email, jobs)

    if "error" in result:
        return jsonify(result), 500

    return jsonify(result)


def run_server(host: str = "127.0.0.1", port: int = 5000, outputs_dir: Optional[Path] = None, debug: bool = False):
    """Run the Flask server."""
    if outputs_dir:
        app.config["OUTPUTS_DIR"] = outputs_dir

    init_feedback_db()

    print(f"\n{'='*60}")
    print("  Role Radar - Job Review UI")
    print(f"{'='*60}")
    print(f"\n  Open in your browser: http://{host}:{port}")
    print("\n  - View and filter jobs")
    print("  - Like/dislike to train preferences")
    print("  - Export learned preferences to YAML")
    print(f"\n  Press Ctrl+C to stop\n{'='*60}\n")

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run_server(debug=True)
