"""Tests for job scoring module."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from role_radar.config import Preferences, TitleRule
from role_radar.models import (
    CompanyType,
    CVSignals,
    Job,
    JobLocation,
    ATSType,
)
from role_radar.scoring import (
    detect_job_seniority,
    is_pm_title,
    should_exclude_job,
    matches_location,
    filter_jobs,
    JobScorer,
    score_and_rank_jobs,
)


@pytest.fixture
def sample_cv_signals():
    return CVSignals(
        raw_text="Test CV",
        recent_titles=["Senior Product Manager"],
        all_titles=["Senior Product Manager", "Product Manager"],
        skills=["sql", "python", "machine learning", "roadmap", "agile"],
        domains=["ai/ml", "b2b saas"],
        keywords=["product", "data", "analysis"],
        inferred_seniority="Senior PM",
        years_experience=6,
    )


@pytest.fixture
def sample_preferences():
    return Preferences(
        location="San Francisco Bay Area",
        include_remote=True,
        seniority=["PM", "Senior PM"],
        allowed_titles=["Product Manager", "PM", "Senior PM", "Sr. PM"],
        excluded_keywords=["Sales", "Marketing"],
        boost_ai_companies=True,
        boost_vc_backed=True,
    )


@pytest.fixture
def sample_job():
    return Job(
        id="test_123",
        external_id="123",
        company="TestCo",
        company_slug="testco",
        company_type=CompanyType.AI_TOP_20,
        title="Senior Product Manager, AI Platform",
        location=JobLocation(
            city="San Francisco",
            state="CA",
            remote=False,
            hybrid=True,
            raw_location="San Francisco, CA (Hybrid)",
        ),
        apply_url="https://example.com/apply",
        department="Product",
        source_ats=ATSType.GREENHOUSE,
    )


class TestDetectJobSeniority:
    def test_detects_senior_pm(self):
        assert detect_job_seniority("Senior Product Manager") == "Senior PM"
        assert detect_job_seniority("Sr. PM, AI Platform") == "Senior PM"

    def test_detects_regular_pm(self):
        assert detect_job_seniority("Product Manager") == "PM"
        assert detect_job_seniority("PM - Growth") == "PM"

    def test_detects_apm(self):
        assert detect_job_seniority("Associate Product Manager") == "APM"
        assert detect_job_seniority("APM, Consumer") == "APM"

    def test_detects_group_pm(self):
        assert detect_job_seniority("Group Product Manager") == "Group PM"

    def test_detects_director(self):
        assert detect_job_seniority("Director of Product") == "Director"

    def test_returns_none_for_non_pm(self):
        assert detect_job_seniority("Software Engineer") is None


class TestIsPmTitle:
    def test_matches_exact_title(self):
        allowed = ["Product Manager", "PM"]
        assert is_pm_title("Product Manager", allowed) is True

    def test_matches_partial_title(self):
        allowed = ["Product Manager"]
        assert is_pm_title("Senior Product Manager, AI", allowed) is True

    def test_rejects_non_pm_title(self):
        allowed = ["Product Manager", "PM"]
        assert is_pm_title("Software Engineer", allowed) is False

    def test_case_insensitive(self):
        allowed = ["Product Manager"]
        assert is_pm_title("PRODUCT MANAGER", allowed) is True


class TestShouldExcludeJob:
    def test_excludes_by_title(self, sample_job):
        sample_job.title = "Sales Product Manager"
        assert should_exclude_job(sample_job, ["Sales"]) is True

    def test_excludes_by_department(self, sample_job):
        sample_job.department = "Marketing"
        assert should_exclude_job(sample_job, ["Marketing"]) is True

    def test_does_not_exclude_matching_job(self, sample_job):
        assert should_exclude_job(sample_job, ["Sales"]) is False


class TestIsNyc:
    def _loc(self, raw, city=None, state=None):
        return JobLocation(raw_location=raw, city=city, state=state)

    def test_new_york_city(self):
        assert self._loc("New York, NY", city="New York", state="NY").is_nyc() is True

    def test_manhattan(self):
        assert self._loc("Manhattan, NY").is_nyc() is True

    def test_brooklyn(self):
        assert self._loc("Brooklyn, NY").is_nyc() is True

    def test_nyc_abbrev(self):
        assert self._loc("NYC").is_nyc() is True

    def test_jersey_city_counted_as_metro(self):
        assert self._loc("Jersey City, NJ").is_nyc() is True

    def test_sf_not_nyc(self):
        assert self._loc("San Francisco, CA").is_nyc() is False

    def test_albany_not_nyc(self):
        # State code NY alone shouldn't promote upstate to NYC
        assert self._loc("Albany, NY", city="Albany", state="NY").is_nyc() is False


class TestMatchesLocation:
    def test_matches_sf_bay_area(self, sample_job):
        assert matches_location(sample_job, "San Francisco Bay Area", True) is True

    def test_matches_remote(self, sample_job):
        sample_job.location.remote = True
        assert matches_location(sample_job, "New York", True) is True

    def test_rejects_non_matching(self, sample_job):
        sample_job.location.raw_location = "New York, NY"
        sample_job.location.city = "New York"
        assert matches_location(sample_job, "San Francisco Bay Area", False) is False

    def test_matches_nyc(self, sample_job):
        sample_job.location.raw_location = "New York, NY"
        sample_job.location.city = "New York"
        sample_job.location.state = "NY"
        assert matches_location(sample_job, "New York", False) is True

    def test_matches_nyc_boroughs(self, sample_job):
        sample_job.location.raw_location = "Brooklyn, NY"
        assert matches_location(sample_job, "New York", False) is True

    def test_matches_multi_location_sf(self, sample_job):
        # SF job, both regions in preferences — should match
        assert matches_location(sample_job, ["San Francisco Bay Area", "New York"], False) is True

    def test_matches_multi_location_nyc(self, sample_job):
        sample_job.location.raw_location = "Manhattan, NY"
        sample_job.location.city = "New York"
        sample_job.location.state = "NY"
        assert matches_location(sample_job, ["San Francisco Bay Area", "New York"], False) is True

    def test_multi_location_rejects_other_region(self, sample_job):
        sample_job.location.raw_location = "Austin, TX"
        sample_job.location.city = "Austin"
        sample_job.location.state = "TX"
        assert matches_location(sample_job, ["San Francisco Bay Area", "New York"], False) is False

    def test_albany_does_not_match_nyc(self, sample_job):
        # ", NY" state code alone shouldn't match NYC — only NYC cities do
        sample_job.location.raw_location = "Albany, NY"
        sample_job.location.city = "Albany"
        sample_job.location.state = "NY"
        assert matches_location(sample_job, "New York", False) is False


class TestJobScorer:
    def test_scores_title_seniority(self, sample_cv_signals, sample_preferences, sample_job):
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score, detail = scorer._score_title_seniority(sample_job)
        assert 0 <= score <= 25
        assert "Senior PM" in detail

    def test_scores_skills_overlap(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.title = "Product Manager - SQL, Python, Machine Learning"
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score, skills = scorer._score_skills_overlap(sample_job)
        assert 0 <= score <= 35
        assert len(skills) > 0

    def test_scores_domain_overlap(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.title = "Product Manager - AI Platform"
        sample_job.department = "AI/ML"
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score, domains = scorer._score_domain_overlap(sample_job)
        assert 0 <= score <= 25

    def test_scores_location_fit(self, sample_cv_signals, sample_preferences, sample_job):
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score = scorer._score_location_fit(sample_job)
        assert 0 <= score <= 10
        assert score >= 8  # SF Bay Area should score high

    def test_scores_nyc_when_nyc_targeted(self, sample_cv_signals, sample_job):
        prefs = Preferences(
            location=["San Francisco Bay Area", "New York"],
            include_remote=False,
        )
        sample_job.location.raw_location = "New York, NY"
        sample_job.location.city = "New York"
        sample_job.location.state = "NY"
        scorer = JobScorer(sample_cv_signals, prefs)
        assert scorer._score_location_fit(sample_job) == 10.0

    def test_scores_nyc_lower_when_only_sf_targeted(self, sample_cv_signals, sample_job):
        prefs = Preferences(location="San Francisco Bay Area", include_remote=False)
        sample_job.location.raw_location = "New York, NY"
        sample_job.location.city = "New York"
        sample_job.location.state = "NY"
        scorer = JobScorer(sample_cv_signals, prefs)
        # NYC still recognized but no boost since not targeted
        assert scorer._score_location_fit(sample_job) == 8.0

    def test_scores_company_preference(self, sample_cv_signals, sample_preferences, sample_job):
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score = scorer._score_company_preference(sample_job)
        assert 0 <= score <= 5
        assert score >= 3  # AI_TOP_20 should get boost

    def test_full_score(self, sample_cv_signals, sample_preferences, sample_job):
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        scored = scorer.score_job(sample_job)
        assert 0 <= scored.score <= 100
        assert scored.rank == 0  # Rank not set yet
        assert scored.score_breakdown.total == scored.score


class TestFilterJobs:
    def test_filters_by_title(self, sample_job, sample_preferences):
        jobs = [sample_job]
        filtered = filter_jobs(jobs, sample_preferences)
        assert len(filtered) == 1

    def test_excludes_non_pm(self, sample_job, sample_preferences):
        sample_job.title = "Software Engineer"
        jobs = [sample_job]
        filtered = filter_jobs(jobs, sample_preferences)
        assert len(filtered) == 0

    def test_excludes_by_keyword(self, sample_job, sample_preferences):
        sample_job.title = "Sales Product Manager"
        jobs = [sample_job]
        filtered = filter_jobs(jobs, sample_preferences)
        assert len(filtered) == 0


class TestScoreAndRankJobs:
    def test_scores_and_ranks(self, sample_cv_signals, sample_preferences, sample_job):
        job2 = Job(
            id="test_456",
            external_id="456",
            company="TestCo2",
            company_slug="testco2",
            company_type=CompanyType.VC_BACKED,
            title="Product Manager",
            location=JobLocation(raw_location="Remote", remote=True),
            apply_url="https://example.com/apply2",
            source_ats=ATSType.LEVER,
        )

        jobs = [sample_job, job2]
        scored = score_and_rank_jobs(jobs, sample_cv_signals, sample_preferences)

        assert len(scored) == 2
        assert scored[0].rank == 1
        assert scored[1].rank == 2
        assert scored[0].score >= scored[1].score

    def test_limits_results(self, sample_cv_signals, sample_preferences, sample_job):
        jobs = [sample_job] * 20
        scored = score_and_rank_jobs(jobs, sample_cv_signals, sample_preferences, max_results=5)
        assert len(scored) == 5


class TestSeniorityGapPenalties:
    """A Senior PM CV should NOT score a Staff/Principal role highly anymore."""

    def test_too_senior_role_scored_lower(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.title = "Staff Product Manager"  # Staff = level 7, CV Senior PM = level 5
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score, _ = scorer._score_title_seniority(sample_job)
        # Old behavior: 18.0 (level_diff=2, "moderate"). New: 16.0 (too senior penalty).
        assert score <= 18.0

    def test_principal_role_heavily_penalized(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.title = "Principal Product Manager"  # Principal = 8, diff=3
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score, detail = scorer._score_title_seniority(sample_job)
        assert score <= 12.0  # Was 18.0 (gap<=4) under old logic

    def test_vp_role_zero_score(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.title = "VP Product"  # VP = 12, diff=7
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score, detail = scorer._score_title_seniority(sample_job)
        assert score == 0.0
        assert "too senior" in detail.lower()

    def test_exact_match_still_full_score(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.title = "Senior Product Manager"
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        score, _ = scorer._score_title_seniority(sample_job)
        assert score == 25.0


class TestRecencyMultiplier:
    """Stale jobs should rank below fresh ones."""

    def _scorer(self, cv, prefs):
        return JobScorer(cv, prefs, use_learned_preferences=False)

    def test_fresh_job_full_multiplier(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.posted_date = datetime.now(timezone.utc) - timedelta(days=2)
        mult, _ = self._scorer(sample_cv_signals, sample_preferences)._score_recency(sample_job)
        assert mult == 1.0

    def test_month_old_job_dampened(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.posted_date = datetime.now(timezone.utc) - timedelta(days=20)
        mult, _ = self._scorer(sample_cv_signals, sample_preferences)._score_recency(sample_job)
        assert mult == 0.9

    def test_quarter_old_job_heavily_dampened(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.posted_date = datetime.now(timezone.utc) - timedelta(days=60)
        mult, _ = self._scorer(sample_cv_signals, sample_preferences)._score_recency(sample_job)
        assert mult == 0.7

    def test_ancient_job_half_score(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.posted_date = datetime.now(timezone.utc) - timedelta(days=180)
        mult, _ = self._scorer(sample_cv_signals, sample_preferences)._score_recency(sample_job)
        assert mult == 0.5

    def test_no_date_assumed_fresh(self, sample_cv_signals, sample_preferences, sample_job):
        sample_job.posted_date = None
        mult, _ = self._scorer(sample_cv_signals, sample_preferences)._score_recency(sample_job)
        assert mult == 1.0

    def test_naive_datetime_treated_as_utc(self, sample_cv_signals, sample_preferences, sample_job):
        # posted_date can be tz-naive depending on the connector
        sample_job.posted_date = datetime.utcnow() - timedelta(days=5)
        mult, _ = self._scorer(sample_cv_signals, sample_preferences)._score_recency(sample_job)
        assert mult == 1.0

    def test_full_score_includes_recency_dampening(self, sample_cv_signals, sample_preferences, sample_job):
        scorer = self._scorer(sample_cv_signals, sample_preferences)
        sample_job.posted_date = datetime.now(timezone.utc) - timedelta(days=2)
        fresh = scorer.score_job(sample_job).score

        sample_job.posted_date = datetime.now(timezone.utc) - timedelta(days=180)
        stale = scorer.score_job(sample_job).score

        assert fresh > stale
        assert stale <= fresh * 0.55  # ~half multiplier


class TestLearnedPreferences:
    """The feedback loop directly shapes ranking — needs coverage."""

    @pytest.fixture
    def feedback_db(self, tmp_path, monkeypatch):
        """Stand up a feedback DB at the path load_learned_preferences() reads from."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        db_dir = fake_home / ".role_radar"
        db_dir.mkdir()
        db_path = db_dir / "feedback.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE learned_preferences (
                preference_type TEXT,
                preference_key TEXT,
                weight_adjustment REAL,
                sample_count INTEGER
            )
        """)
        return conn

    def test_no_feedback_db_returns_neutral(self, sample_cv_signals, sample_preferences, sample_job, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        scorer = JobScorer(sample_cv_signals, sample_preferences)
        adj, reasons = scorer._score_learned_preference(sample_job)
        assert adj == 0.0
        assert reasons == []

    def test_liked_company_boosts_score(self, sample_cv_signals, sample_preferences, sample_job, feedback_db):
        feedback_db.execute(
            "INSERT INTO learned_preferences VALUES (?, ?, ?, ?)",
            ("company", "TestCo", 1.0, 5),
        )
        feedback_db.commit()

        scorer = JobScorer(sample_cv_signals, sample_preferences)
        adj, reasons = scorer._score_learned_preference(sample_job)
        assert adj > 0
        assert any("liked" in r.lower() for r in reasons)

    def test_disliked_company_penalizes_score(self, sample_cv_signals, sample_preferences, sample_job, feedback_db):
        feedback_db.execute(
            "INSERT INTO learned_preferences VALUES (?, ?, ?, ?)",
            ("company", "TestCo", -1.0, 5),
        )
        feedback_db.commit()

        scorer = JobScorer(sample_cv_signals, sample_preferences)
        adj, reasons = scorer._score_learned_preference(sample_job)
        assert adj < 0
        assert any("disliked" in r.lower() for r in reasons)

    def test_low_sample_count_dampens_adjustment(self, sample_cv_signals, sample_preferences, sample_job, feedback_db):
        # 1 sample → confidence 1/3, adjustment scaled down
        feedback_db.execute(
            "INSERT INTO learned_preferences VALUES (?, ?, ?, ?)",
            ("company", "TestCo", 1.0, 1),
        )
        feedback_db.commit()

        scorer = JobScorer(sample_cv_signals, sample_preferences)
        low_conf, _ = scorer._score_learned_preference(sample_job)

        # Replace with high sample count
        feedback_db.execute("DELETE FROM learned_preferences")
        feedback_db.execute(
            "INSERT INTO learned_preferences VALUES (?, ?, ?, ?)",
            ("company", "TestCo", 1.0, 10),
        )
        feedback_db.commit()

        scorer2 = JobScorer(sample_cv_signals, sample_preferences)
        high_conf, _ = scorer2._score_learned_preference(sample_job)

        assert high_conf > low_conf

    def test_adjustment_clamped_to_ten(self, sample_cv_signals, sample_preferences, sample_job, feedback_db):
        # Stack multiple strong signals — should still cap at +10
        feedback_db.execute(
            "INSERT INTO learned_preferences VALUES (?, ?, ?, ?)",
            ("company", "TestCo", 5.0, 100),
        )
        feedback_db.commit()

        scorer = JobScorer(sample_cv_signals, sample_preferences)
        adj, _ = scorer._score_learned_preference(sample_job)
        assert -10.0 <= adj <= 10.0


class TestComposableRules:
    """include_rules / exclude_rules should override flat substring lists when present."""

    def test_include_rule_any_of_matches(self):
        rules = [TitleRule(any_of=["ai pm", "ai product manager"])]
        assert is_pm_title("AI Product Manager", [], include_rules=rules) is True
        assert is_pm_title("Software Engineer", [], include_rules=rules) is False

    def test_include_rule_all_of_requires_every_term(self):
        rules = [TitleRule(all_of=["product manager", "ai"])]
        assert is_pm_title("AI Product Manager", [], include_rules=rules) is True
        assert is_pm_title("Senior Product Manager", [], include_rules=rules) is False

    def test_include_rule_combines_all_of_and_any_of(self):
        rules = [TitleRule(all_of=["product manager"], any_of=["ai", "ml"])]
        assert is_pm_title("AI Product Manager", [], include_rules=rules) is True
        assert is_pm_title("ML Product Manager", [], include_rules=rules) is True
        assert is_pm_title("Growth Product Manager", [], include_rules=rules) is False

    def test_exclude_rule_filters_seniority(self, sample_job):
        rules = [TitleRule(any_of=["staff", "principal"])]
        sample_job.title = "Staff Product Manager"
        assert should_exclude_job(sample_job, [], exclude_rules=rules) is True

        sample_job.title = "Senior Product Manager"
        assert should_exclude_job(sample_job, [], exclude_rules=rules) is False

    def test_rules_override_legacy_lists(self, sample_job):
        # allowed_titles would match, but include_rules is stricter
        rules = [TitleRule(any_of=["ai pm"])]
        sample_job.title = "Senior Product Manager"
        assert is_pm_title(sample_job.title, ["product manager"], include_rules=rules) is False

    def test_empty_rule_never_matches(self):
        rules = [TitleRule()]
        assert is_pm_title("anything", [], include_rules=rules) is False
