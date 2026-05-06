# Company-discovery improvements for Role Radar

A read of the current state and concrete improvements to find more relevant companies (and more relevant *roles* at those companies). Ordered by ROI per implementation hour.

## Current state

Role Radar gets companies from three places:

1. **`AI_COMPANIES_SEED`** — a hand-curated list of ~80 AI companies in [`ai_top20.py`](../src/role_radar/company_sources/ai_top20.py). Last manually reviewed `2026-04-25`. A staleness warning fires after 60 days.
2. **`KNOWN_COMPANY_ATS`** — a hand-maintained dict of ~100 company-to-ATS mappings in [`vc_portfolios.py`](../src/role_radar/company_sources/vc_portfolios.py).
3. **`portfolios.csv`** — user-supplied list of VC-backed companies with manually-set careers URLs.
4. **VC portfolio scraping** — disabled by default (`scrape_portfolios=False` in [`main.py:241`](../src/role_radar/main.py)) "for reliability."

Connectors exist for Greenhouse, Lever, Ashby, SmartRecruiters, and a generic-HTML fallback. Workday, Eightfold, Phenom, and other custom platforms fall through to the generic scraper, which usually fails.

The match score (100 points) is keyword-overlap on title/skills/domains, plus location and a small AI-company boost. There's a learned-preferences layer ([`scoring.py:374`](../src/role_radar/scoring.py)) that reads from `~/.role_radar/feedback.db`.

## What's leaking relevant companies

These are the gaps I'd fix in priority order — the first three are the highest-leverage.

### 1. The seed list goes stale faster than the warning fires

`STALE_AFTER_DAYS = 60` is too generous for AI in 2026 — Series A → unicorn cycles run in months, and entirely new frontier labs (e.g. `Thinking Machines Lab`, `Safe Superintelligence`) appear every quarter. By the time the warning fires you've already missed two full hiring cycles.

**Fix (1-2h):** Drop the threshold to 21 days and add a **per-company freshness signal** — if a company was added more than 90 days ago and has had zero matched jobs in the last 4 weekly runs, flag for review. Surface stale companies in the `role-radar companies` output.

### 2. VC portfolio scraping is disabled but is the highest-leverage discovery surface

The infrastructure is built ([`vc_portfolios.py:252`](../src/role_radar/company_sources/vc_portfolios.py), `_scrape_portfolio_page`) but `scrape_portfolios=False` is hardcoded in `main.py:241` "for reliability." This means **you only ever discover companies you've already heard of and added to `portfolios.csv`** — exactly the opposite of what the tool should do for someone hunting AI roles in a fast-moving market.

**Fix (3-4h):** Re-enable scraping with a defensive wrapper:
- Add a `vc_scrape_cache.json` keyed by VC URL, refreshed weekly. If a VC's page failed last time, retry but with a longer timeout.
- After scraping, run discovered companies through a quick filter: name length 2-50 chars, looks like a company (not "About" / "Team" / "Blog"), domain isn't on a known-not-a-company allowlist (Notion, Medium, Substack, etc.).
- Pipe each new company through the existing `KNOWN_COMPANY_ATS` lookup + `_find_careers_url` pipeline. Discoveries with no detectable ATS get parked in a `data/discovered_companies.csv` for manual triage instead of clogging the live job fetch.

This single change should 3-5x the company list with a half-day of work. The "unreliable" framing is solvable — the failure mode is that you discover too many false positives, not too few real ones, and triage filtering catches that.

### 3. Workday is missing, and most large AI companies use it

Looking at the seed list, Snowflake (PhenomPeople), NVIDIA (Eightfold), Meta (custom), Apple (custom), Google (custom), Tesla (Workday), Microsoft (custom) all fall to `GENERIC_HTML` which usually doesn't work. The score boosts for "AI Top 20" and "VC-backed" don't matter if you can't fetch the jobs.

**Fix (4-8h):** Add a **WorkdayConnector** (and ideally Eightfold). Workday job boards expose a JSON endpoint like `https://<tenant>.wd5.myworkdayjobs.com/wday/cxs/<tenant>/<board>/jobs` with predictable POST request bodies. Phenom and Eightfold are similar — public scraping is well-documented. Pattern is identical to existing connectors (subclass `BaseConnector`, return `list[Job]`).

Worth checking: how many seed companies in `AI_COMPANIES_SEED` currently have `ats_type=GENERIC_HTML` because the platform isn't supported, vs because we don't know the ATS yet. That's the size of the addressable bug.

### 4. Title scoring is keyword-overlap, not semantic

[`scoring.py:249`](../src/role_radar/scoring.py) (`_score_skills_overlap`) does substring matching: a CV with "machine learning" matches a JD with "ML platform" only if both literally contain the same string. Real PM JDs use synonyms heavily — "platform PM," "infrastructure PM," "AI platform PM," "developer platform PM" all describe overlapping work but won't cross-match.

**Fix (2h, no API call):** Build a small **synonym dictionary** in [`scoring.py`](../src/role_radar/scoring.py) — `{"infrastructure": ["infra", "platform", "systems"], "ai": ["ml", "machine learning", "llm", "genai"]}` — and expand both CV signals and JD text through it before matching. Also: deduplicate jobs by `(company, normalized_title)` instead of just title — currently a "Senior PM, Infrastructure" and a "Senior PM, Platform" at the same company show up as two roles when they're often the same opening relisted.

**Fix (8h, with API call):** Optionally — when `ROLE_RADAR_USE_OPENAI_FOR_SCORING=true` (already plumbed in [`config.py:141`](../src/role_radar/config.py)) — run a Claude Haiku 4.5 pass that scores semantic match on title+description vs CV. ~$0.001 per job, would catch the "Senior Product Manager, Cortex AI Platform" job at Snowflake that the keyword scorer misses.

### 5. The 6-year experience filter is a sledgehammer

[`scoring.py:532`](../src/role_radar/scoring.py) (`exceeds_experience_requirement`) drops any job that mentions ">6 years" anywhere in the description. This is over-aggressive — many PM JDs include the line "5+ years preferred, 8+ years for Staff" which gets the 5-year role dropped because the regex matches "8".

**Fix (1h):** Make the threshold preference-driven (`max_years_experience: 8` in `preferences.yaml`), and change the regex to only catch the "required" years — look for `\b(\d+)\+?\s*years?\b` in proximity to `required|minimum|must have`, not in proximity to `preferred|nice to have`.

### 6. No discovery from "where AI PMs are actually hiring" signals

Beyond the curated seed list, the highest-signal source for "where should an AI PM look in May 2026" is people in those roles posting on LinkedIn, the YC W26/S26 batches, the Information AI 50, Forbes AI 50, and a16z's recent AI investments page. None of these are wired in.

**Fix (4-6h, optional v2):** Add a `role-radar discover` command that pulls from:
- **YC company directory** — public JSON at `https://www.ycombinator.com/companies/api?industry=AI`
- **The Information AI 50 / Forbes AI 50** — annual lists, scrapable from the published article pages
- **a16z portfolio AI tag** — `https://a16z.com/portfolio/?_industries=ai-ml`
- **Greenhouse / Lever board indexes** — both have public listings of all boards using their service; can grep for AI-related company names

Each source feeds into the same `discovered_companies.csv` triage flow as #2.

### 7. Match feedback is per-job, not per-company-type

[`web/app.py:141`](../src/role_radar/web/app.py) (`update_learned_preferences`) learns from individual job feedback. But if a user's CV concentrates in a particular AI category (infra, apps, dev tools), the feedback signal could push the *category-level* boost up — "you've liked 8/10 AI Infra companies, deprioritize AI Apps" — instead of needing to learn each company individually.

**Fix (3h):** Add a `category` learning signal alongside the existing company / title-keyword ones. When feedback comes in, also update `learned_preferences[category:ai_infra]` based on the company's `AICategory`. Apply at scoring time as a small +/-3 adjustment on `_score_company_preference`.

## What I'd do this week

If I had one week of evening time, in order:
1. Re-enable VC portfolio scraping with the cache+filter wrapper (#2) — biggest discovery lift
2. Add the Workday connector (#3) — unlocks ~10 seed-list companies you can't currently fetch
3. Add the synonym dictionary (#4 first half) — fixes the silent under-matching
4. Drop staleness threshold + per-company freshness flag (#1) — keeps the seed list honest
5. Loosen the experience filter (#5) — quick win

Skip the `discover` command and the OpenAI semantic scorer until #2-4 prove the discovery surface is actually the bottleneck.
