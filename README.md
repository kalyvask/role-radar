# Role Radar 🎯

End-to-end PM job hunt for AI companies: find relevant roles, score them against your CV, then generate a candidate-tailored LLM interview prep doc for any one with a single click.

Two workflows in one tool:

1. **Discovery** — pulls live job postings from ~80 curated AI companies (frontier labs, AI infra, AI apps, dev tools) and VC-backed startups across Greenhouse, Lever, Ashby, SmartRecruiters, and a generic HTML fallback. Scores each role 0–100 against your CV across title/seniority, skills, domains, location, and company preference. Reads from a local SQLite cache, so you can replay scoring without re-fetching.
2. **Interview prep** — for any job in the latest report, generate a comprehensive prep report covering the company, the role, and the likely interview questions for it (pulled from public signals about how that company interviews), then auto-adjust which of your CV stories to tell so each one maps to what the role actually wants. Built on Claude Opus 4.7 with adaptive thinking, structured Pydantic output, and prompt-cached static context (frameworks, calibrations, per-company playbooks). Streams live progress to the UI button (parsing → calling Claude → reviewing → writing files), runs a second-pass critic that scores the doc 1–10 with severity-tagged findings, and auto-opens the result as a styled HTML view + downloadable Word file.

This repo ships clean — no CV, no name, no personal data. See [Personalize this for your own use](#personalize-this-for-your-own-use) for the 5-minute setup.

## Personalize this for your own use

Forking or cloning? Do these steps in order before running anything:

1. **Drop your CV into the repo root.** Any filename matching `*_CV.pdf`, `*_Resume.pdf`, or `*_resume.pdf` is auto-gitignored (see `.gitignore` lines 52–54). Example: `Alex_Kalyvas_CV.pdf`. For DOCX/TXT, add the filename to `.gitignore` manually.

2. **Create your `.env`** from the template:

   ```bash
   cp .env.example .env
   ```

   Then add these three lines (the email credentials are also required if you want `--send` to work):

   ```bash
   ROLE_RADAR_CV_PATH=/absolute/path/to/Your_Name_CV.pdf
   ROLE_RADAR_CANDIDATE_NAME=Your Name
   ANTHROPIC_API_KEY=sk-ant-...   # only needed for the interview prep generator
   ```

   Get an Anthropic key at <https://console.anthropic.com/settings/keys>.

3. **Edit `preferences.yaml`** for your search — set `location`, `include_remote`, `seniority`, `allowed_titles`, and `excluded_keywords`. The shipped defaults target SF Bay Area PM roles; change them to match yours.

4. **(Optional) Edit `data/portfolios.csv`** to add custom companies you want tracked beyond the curated AI Top 20 / VC portfolios. Schema: `company_name,homepage_url,careers_url,vc_backers,notes`.

5. **(Windows users) Wire up the local run scripts.** Copy the templates and edit the paths inside:

   ```bash
   cp run-scrape.bat.example run-scrape.bat   # edit cd path + CV_PATH
   cp start-ui.bat.example   start-ui.bat     # edit cd path
   ```

   Both `.bat` files are gitignored (see `.gitignore` lines 58–59) so your local paths never get committed. Point Windows Task Scheduler at `run-scrape.bat` for weekly automated scrapes.

6. **What stays local** (none of this is ever committed): your CV PDF, your `.env`, generated prep docs in `outputs/prep/`, learned like/dislike feedback in `~/.role_radar/feedback.db` (lives outside the repo, in your home dir), and your `*.bat` scripts.

You're done. Run `role-radar run "$ROLE_RADAR_CV_PATH"` for a dry run, or jump to [Installation](#installation) below.

## Features

- **Curated company lists** — transparent scoring methodology for Top 20 AI companies and Top VCs
- **Multi-ATS support** — connectors for Greenhouse, Lever, Ashby, SmartRecruiters, generic HTML
- **Smart matching** — CV-based scoring across title/seniority, skills, domains, location, with a learned-preferences layer driven by like/dislike feedback
- **Web review UI** — Flask UI to browse matches, like/dislike to train the scorer, take notes, mark applied
- **Interview prep generator** — Claude Opus 4.7 with adaptive thinking, prompt-cached static context (frameworks, calibrations, per-company playbooks), structured Pydantic output, second-pass review critic, live SSE progress streaming, Markdown + DOCX output
- **Email reports** — HTML emails with match rationale and score breakdowns
- **Local caching** — SQLite for jobs/companies, prompt cache for the LLM
- **Observability** — structured JSON logging and run summaries

## Installation

### Prerequisites

- Python 3.11 or higher
- pip or pipx

### Install from source

```bash
cd role-radar
pip install -e ".[dev]"
```

### Quick Setup

```bash
# Initialize configuration files
role-radar init

# Edit .env with your email credentials
# Edit preferences.yaml for your job search

# Run in test mode (no email sent)
role-radar run path/to/your/cv.pdf

# Run and send email
role-radar run path/to/your/cv.pdf --send
```

## Configuration

### Environment Variables (.env)

Copy `.env.example` to `.env` and configure:

```bash
# Email settings (choose one provider)
ROLE_RADAR_EMAIL_PROVIDER=smtp  # smtp or sendgrid

# SMTP settings (for Gmail, use an App Password)
ROLE_RADAR_SMTP_HOST=smtp.gmail.com
ROLE_RADAR_SMTP_PORT=587
ROLE_RADAR_SMTP_USERNAME=your-email@gmail.com
ROLE_RADAR_SMTP_PASSWORD=your-app-password

# Email addresses
ROLE_RADAR_EMAIL_FROM=your-email@gmail.com
ROLE_RADAR_EMAIL_TO=your-email@gmail.com

# Test mode (prints email instead of sending)
ROLE_RADAR_EMAIL_TEST_MODE=true
```

### Preferences (preferences.yaml)

Customize your job search:

```yaml
location: "San Francisco Bay Area"
include_remote: true

seniority:
  - "PM"
  - "Senior PM"
  - "Staff PM"

allowed_titles:
  - "Product Manager"
  - "Technical Product Manager"
  - "AI Product Manager"
  - "Senior Product Manager"

excluded_keywords:
  - "Sales"
  - "Marketing"

max_roles_per_email: 15
```

### Portfolio Companies (data/portfolios.csv)

Add custom VC-backed companies:

```csv
company_name,homepage_url,careers_url,vc_backers,notes
Glean,https://www.glean.com,https://www.glean.com/careers,"Sequoia, Kleiner Perkins",Enterprise AI search
Harvey,https://www.harvey.ai,https://www.harvey.ai/careers,"Sequoia",Legal AI
```

## Usage

### CLI Commands

```bash
# Initialize configuration files
role-radar init

# Run job search (test mode - no email sent)
role-radar run path/to/cv.pdf

# Run with custom preferences
role-radar run path/to/cv.pdf --prefs my-preferences.yaml

# Run and send email
role-radar run path/to/cv.pdf --send

# Daily run mode (for cron)
role-radar run path/to/cv.pdf --daily

# Use cached jobs (skip fetching)
role-radar run path/to/cv.pdf --skip-fetch

# Show company lists
role-radar companies

# Debug a specific company
role-radar debug "OpenAI"
```

### Daily Cron Setup

Add to your crontab (`crontab -e`):

```bash
# Run Role Radar daily at 9 AM
0 9 * * * cd /path/to/role-radar && /path/to/venv/bin/role-radar run /path/to/cv.pdf --daily >> /var/log/role-radar.log 2>&1
```

## Scoring Methodology

### AI Top 20 Companies (100 points)

| Dimension | Max Points | Description |
|-----------|------------|-------------|
| Company Category | 25 | Frontier Lab (25), AI Infra (20), AI Apps (15) |
| Technical Reputation | 20 | GitHub stars, benchmarks, research output |
| Funding/Scale | 20 | Total funding or public company status |
| Hiring Velocity | 15 | Open roles count, growth signals |
| Developer Adoption | 10 | API usage, community size |
| Recent Momentum | 10 | Product launches, major announcements |

### Top VCs (100 points)

| Dimension | Max Points | Description |
|-----------|------------|-------------|
| Track Record | 35 | Unicorn count, notable exits |
| Fund Size/AUM | 25 | Assets under management |
| Stage Focus | 20 | Seed/Early (20), Growth (12) |
| SF Concentration | 10 | Portfolio focus on Bay Area |
| Recent Activity | 10 | Deals per year |

### Job Matching (100 points)

| Dimension | Max Points | Description |
|-----------|------------|-------------|
| Title/Seniority | 25 | Match between CV and job seniority |
| Skills Overlap | 35 | Technical and product skills match |
| Domain Overlap | 25 | Industry/domain expertise alignment |
| Location Fit | 10 | Geographic and remote preferences |
| Company Preference | 5 | Boost for AI/VC-backed companies |

## Output Files

After each run, find these files in `outputs/`:

- `report_YYYYMMDD_HHMMSS.json` - Detailed JSON report
- `report_YYYYMMDD_HHMMSS.html` - Visual HTML report
- `email_RUNID.html` - Email preview (test mode)
- `ai_top20_scoring.md` - AI company scoring breakdown
- `top_vcs_scoring.md` - VC scoring breakdown

## Legal & Ethical Considerations

Role Radar is designed to be respectful of websites and APIs:

- **No LinkedIn scraping** - We don't scrape LinkedIn or any site that prohibits it
- **robots.txt compliance** - All HTML parsing respects robots.txt
- **Rate limiting** - Built-in rate limiter (2 req/sec default)
- **Official APIs only** - Uses official ATS APIs (Greenhouse, Lever, etc.)
- **User-agent identification** - Identifies itself properly
- **Caching** - SQLite cache reduces redundant requests

## Troubleshooting

### No jobs found

1. Check that companies have valid ATS identifiers in the seed data
2. Verify your network can reach the job board APIs
3. Try `role-radar debug "CompanyName"` to test a specific company

### Email not sending

1. Ensure `.env` has correct credentials
2. For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833)
3. Test with `ROLE_RADAR_EMAIL_TEST_MODE=true` first

### Low match scores

1. Review your CV to ensure skills are clearly listed
2. Adjust `preferences.yaml` to match your experience level
3. Check that domains in your CV align with target companies

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=role_radar --cov-report=html

# Format code
black src tests
ruff check src tests

# Type checking
mypy src
```

## Architecture

```
src/role_radar/
├── main.py              # Typer CLI entry point
├── config.py            # Settings and preferences
├── cv_parser.py         # CV/resume parsing
├── company_sources/     # Company list generation
│   ├── ai_top20.py      # AI company scoring
│   ├── top_vcs.py       # VC scoring
│   └── vc_portfolios.py # Portfolio discovery
├── connectors/          # Job board connectors
│   ├── greenhouse.py
│   ├── lever.py
│   ├── smartrecruiters.py
│   └── generic_html.py
├── scoring.py           # Job-CV matching
├── dedupe.py            # Deduplication
├── storage.py           # SQLite caching
├── emailer.py           # Email sending
├── reporting.py         # Report generation
└── utils/
    ├── http.py          # HTTP client with rate limiting
    └── logging.py       # Structured logging
```

## Interview Prep Generator

Once Role Radar finds a relevant role, generate a comprehensive interview prep report with one click. The report covers: a senior advisor's read of the company and role, the technical topics you need to know cold (sourced from the company's public engineering surface), the 5-7 likely interview questions for this role (with sample answers that re-map your CV stories to what this role actually wants), an honest read of how your background maps to the role and where the gaps are, a multi-day prep plan, and the strongest single move to make in the room. Markdown + DOCX output, plus a styled in-browser view.

### Setup

1. Install the optional `interview` extra:

   ```bash
   pip install -e ".[interview]"   # adds the anthropic SDK
   ```

2. Add your Anthropic API key to `.env`:

   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   ROLE_RADAR_CV_PATH=/absolute/path/to/your/cv.pdf
   ROLE_RADAR_CANDIDATE_NAME=Your Name
   ```

   Get a key at https://console.anthropic.com/settings/keys.

### Usage

**From the Web UI** — click the **📄 Generate prep** button on any job card. The button streams live progress via SSE — `📄 Parsing CV → 🤖 Calling Claude (~60s) · Ns → 🔍 Reviewing doc (~30s) · Ns → 💾 Writing files` — and once done transforms into clickable **📖 Open · ⬇ DOCX** links to the just-generated files. The Markdown also auto-opens in a new tab. Total time: 60-120s.

**From the CLI** — run against the latest report:

```bash
# Top match
role-radar prep cv.pdf --rank 1

# Top 3 matches (one doc each)
role-radar prep cv.pdf --top 3

# Specific job by ID
role-radar prep cv.pdf --job-id cursor_66e67c2e

# Custom output dir, skip DOCX
role-radar prep cv.pdf --rank 1 --output-dir ./my-prep --no-docx

# Generate + run a second-pass critic (gstack-style review)
role-radar prep cv.pdf --rank 1 --review
```

### How it works

- **Prompt context**: a static system prompt (~5K tokens) bundles the candidate's interview frameworks (DASME, SIGNAL metric cascade, model selection, anti-patterns, safety checklist, the 3 Laws of delivery), 4 calibrated weak/strong answer exemplars, and the company-specific playbook if available. Marked `cache_control: ephemeral` so subsequent jobs at the same company hit the cache.
- **Job + CV**: the per-job user message includes the full job posting, extracted CV signals, and a CV excerpt. Volatile, uncached.
- **Structured output**: Pydantic schema with Markdown leaf strings → renders cleanly to both `.md` and `.docx`.
- **Model**: Claude Opus 4.7 with adaptive thinking, `effort=high`, `max_tokens=16000`.

The static context (`data/interview_prep/`) is mirrored from [kalyvask/interview-prep](https://github.com/kalyvask/interview-prep). To refresh it after the source repo updates, re-export the TypeScript content files to JSON and replace the snapshots in `data/interview_prep/`.

## License

MIT

## Contributing

Contributions welcome! Please open an issue first to discuss proposed changes.
