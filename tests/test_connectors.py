"""Tests for job board connectors."""

import pytest
import json
from unittest.mock import Mock, patch
from datetime import datetime

from role_radar.connectors.greenhouse import GreenhouseConnector
from role_radar.connectors.lever import LeverConnector
from role_radar.connectors.workday import WorkdayConnector, parse_workday_identifier
from role_radar.models import Company, CompanyType, ATSType


@pytest.fixture
def mock_http_client():
    return Mock()


@pytest.fixture
def sample_company():
    return Company(
        name="TestCo",
        slug="testco",
        company_type=CompanyType.AI_TOP_20,
        ats_type=ATSType.GREENHOUSE,
        ats_identifier="testco",
    )


class TestGreenhouseConnector:
    def test_parses_jobs_from_api(self, mock_http_client, sample_company):
        # Sample Greenhouse API response
        api_response = {
            "jobs": [
                {
                    "id": 123456,
                    "title": "Senior Product Manager",
                    "location": {"name": "San Francisco, CA"},
                    "absolute_url": "https://boards.greenhouse.io/testco/jobs/123456",
                    "updated_at": "2024-01-15T10:00:00Z",
                    "departments": [{"name": "Product"}],
                },
                {
                    "id": 789012,
                    "title": "Product Manager, AI",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/testco/jobs/789012",
                    "updated_at": "2024-01-14T10:00:00Z",
                    "departments": [{"name": "AI Team"}],
                },
            ]
        }

        mock_http_client.get_json.return_value = api_response

        connector = GreenhouseConnector(mock_http_client)
        jobs = connector.fetch_jobs(sample_company)

        assert len(jobs) == 2
        assert jobs[0].title == "Senior Product Manager"
        assert jobs[0].company == "TestCo"
        assert jobs[0].source_ats == ATSType.GREENHOUSE
        assert "San Francisco" in jobs[0].location.raw_location
        assert jobs[1].location.remote is True

    def test_handles_empty_response(self, mock_http_client, sample_company):
        mock_http_client.get_json.return_value = {"jobs": []}

        connector = GreenhouseConnector(mock_http_client)
        jobs = connector.fetch_jobs(sample_company)

        assert jobs == []

    def test_handles_api_error(self, mock_http_client, sample_company):
        mock_http_client.get_json.side_effect = Exception("API Error")

        connector = GreenhouseConnector(mock_http_client)
        jobs = connector.fetch_jobs(sample_company)

        assert jobs == []


class TestLeverConnector:
    def test_parses_postings_from_api(self, mock_http_client):
        company = Company(
            name="LeverCo",
            slug="leverco",
            company_type=CompanyType.VC_BACKED,
            ats_type=ATSType.LEVER,
            ats_identifier="leverco",
        )

        # Sample Lever API response
        api_response = [
            {
                "id": "abc123",
                "text": "Product Manager",
                "categories": {
                    "location": "San Francisco, CA",
                    "team": "Product",
                },
                "hostedUrl": "https://jobs.lever.co/leverco/abc123",
                "applyUrl": "https://jobs.lever.co/leverco/abc123/apply",
                "createdAt": 1705312800000,  # 2024-01-15
            },
        ]

        mock_http_client.get_json.return_value = api_response

        connector = LeverConnector(mock_http_client)
        jobs = connector.fetch_jobs(company)

        assert len(jobs) == 1
        assert jobs[0].title == "Product Manager"
        assert jobs[0].source_ats == ATSType.LEVER
        assert jobs[0].department == "Product"

    def test_handles_remote_jobs(self, mock_http_client):
        company = Company(
            name="LeverCo",
            slug="leverco",
            company_type=CompanyType.VC_BACKED,
            ats_type=ATSType.LEVER,
            ats_identifier="leverco",
        )

        api_response = [
            {
                "id": "def456",
                "text": "Senior PM",
                "categories": {
                    "location": "Remote - US",
                    "commitment": "Full-time, Remote",
                },
                "hostedUrl": "https://jobs.lever.co/leverco/def456",
            },
        ]

        mock_http_client.get_json.return_value = api_response

        connector = LeverConnector(mock_http_client)
        jobs = connector.fetch_jobs(company)

        assert len(jobs) == 1
        assert "remote" in jobs[0].location.raw_location.lower()


class TestWorkdayConnector:
    @pytest.fixture
    def workday_company(self):
        return Company(
            name="CrowdStrike",
            slug="crowdstrike",
            company_type=CompanyType.AI_TOP_20,
            ats_type=ATSType.WORKDAY,
            ats_identifier="crowdstrike:wd5:crowdstrikecareers",
        )

    def test_parse_identifier_valid(self):
        assert parse_workday_identifier("crowdstrike:wd5:crowdstrikecareers") == (
            "crowdstrike",
            "wd5",
            "crowdstrikecareers",
        )

    def test_parse_identifier_rejects_malformed(self):
        assert parse_workday_identifier("crowdstrike/wd5/board") is None
        assert parse_workday_identifier("only:two") is None
        assert parse_workday_identifier("") is None
        assert parse_workday_identifier("a::b") is None

    def test_parses_jobs_and_paginates(self, mock_http_client, workday_company):
        # First page returns 2 jobs and a total of 3; second page returns the
        # last job. Connector should make two calls and return all 3 jobs.
        page1 = {
            "total": 3,
            "jobPostings": [
                {
                    "title": "Sr. AI Product Manager",
                    "externalPath": "/job/Sunnyvale/Sr-AI-PM/12345",
                    "locationsText": "USA - Sunnyvale, CA",
                    "postedOn": "Posted 3 Days Ago",
                    "bulletFields": ["$200,000 - $250,000"],
                },
                {
                    "title": "Software Engineer",
                    "externalPath": "/job/Remote/SWE/67890",
                    "locationsText": "Remote - USA",
                    "postedOn": "Posted Yesterday",
                    "bulletFields": [],
                },
            ],
        }
        page2 = {
            "total": 3,
            "jobPostings": [
                {
                    "title": "Product Manager, Falcon",
                    "externalPath": "/job/Austin/PM/55555",
                    "locationsText": "USA - Austin, TX",
                    "postedOn": "Posted Today",
                },
            ],
        }
        mock_http_client.post_json.side_effect = [page1, page2]

        connector = WorkdayConnector(mock_http_client)
        jobs = connector.fetch_jobs(workday_company)

        assert len(jobs) == 3
        assert jobs[0].title == "Sr. AI Product Manager"
        assert jobs[0].source_ats == ATSType.WORKDAY
        assert jobs[0].apply_url.endswith("/job/Sunnyvale/Sr-AI-PM/12345")
        assert jobs[0].apply_url.startswith("https://crowdstrike.wd5.myworkdayjobs.com")
        assert jobs[0].salary is not None
        assert jobs[0].salary.min_salary == 200000
        assert jobs[0].salary.max_salary == 250000
        assert jobs[1].location.remote is True
        # Two POSTs: page 0 and page 20
        assert mock_http_client.post_json.call_count == 2
        first_call_kwargs = mock_http_client.post_json.call_args_list[0].kwargs
        second_call_kwargs = mock_http_client.post_json.call_args_list[1].kwargs
        assert first_call_kwargs["json_body"]["offset"] == 0
        assert second_call_kwargs["json_body"]["offset"] == 20

    def test_stops_when_total_reached_in_one_page(self, mock_http_client, workday_company):
        page = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "PM",
                    "externalPath": "/job/SF/PM/1",
                    "locationsText": "USA - San Francisco, CA",
                    "postedOn": "Posted 1 Day Ago",
                },
            ],
        }
        mock_http_client.post_json.return_value = page

        connector = WorkdayConnector(mock_http_client)
        jobs = connector.fetch_jobs(workday_company)

        assert len(jobs) == 1
        assert mock_http_client.post_json.call_count == 1

    def test_stops_on_empty_page(self, mock_http_client, workday_company):
        mock_http_client.post_json.return_value = {"total": 0, "jobPostings": []}

        connector = WorkdayConnector(mock_http_client)
        jobs = connector.fetch_jobs(workday_company)

        assert jobs == []
        assert mock_http_client.post_json.call_count == 1

    def test_stops_on_duplicate_ids(self, mock_http_client, workday_company):
        # API misbehaves: same posting on every page. Connector should detect
        # zero-new-IDs and break instead of looping forever.
        repeating = {
            "total": 999,
            "jobPostings": [
                {"title": "PM", "externalPath": "/job/x/PM/dup", "locationsText": ""},
            ],
        }
        mock_http_client.post_json.return_value = repeating

        connector = WorkdayConnector(mock_http_client)
        jobs = connector.fetch_jobs(workday_company)

        assert len(jobs) == 1
        # Two calls: first returns the new id, second returns the same id
        # (zero-new-in-page) and the loop exits.
        assert mock_http_client.post_json.call_count == 2

    def test_invalid_identifier_returns_empty(self, mock_http_client):
        company = Company(
            name="Bad",
            slug="bad",
            company_type=CompanyType.AI_TOP_20,
            ats_type=ATSType.WORKDAY,
            ats_identifier="not-a-valid-workday-id",
        )
        connector = WorkdayConnector(mock_http_client)
        jobs = connector.fetch_jobs(company)

        assert jobs == []
        assert mock_http_client.post_json.call_count == 0

    def test_handles_api_error(self, mock_http_client, workday_company):
        mock_http_client.post_json.side_effect = Exception("boom")

        connector = WorkdayConnector(mock_http_client)
        jobs = connector.fetch_jobs(workday_company)

        assert jobs == []
