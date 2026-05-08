"""Connector registry for managing ATS connectors."""

from typing import Optional

from role_radar.connectors.base import BaseConnector
from role_radar.connectors.greenhouse import GreenhouseConnector
from role_radar.connectors.lever import LeverConnector
from role_radar.connectors.ashby import AshbyConnector
from role_radar.connectors.smartrecruiters import SmartRecruitersConnector
from role_radar.connectors.workday import WorkdayConnector
from role_radar.connectors.generic_html import GenericHTMLConnector
from role_radar.models import ATSType, Company, Job
from role_radar.utils.http import HTTPClient
from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


class ConnectorRegistry:
    """Registry for managing and selecting job board connectors."""

    def __init__(self, http_client: HTTPClient):
        self.http_client = http_client

        # Initialize connectors
        self._connectors: dict[ATSType, BaseConnector] = {
            ATSType.GREENHOUSE: GreenhouseConnector(http_client),
            ATSType.LEVER: LeverConnector(http_client),
            ATSType.ASHBY: AshbyConnector(http_client),
            ATSType.SMARTRECRUITERS: SmartRecruitersConnector(http_client),
            ATSType.WORKDAY: WorkdayConnector(http_client),
            ATSType.GENERIC_HTML: GenericHTMLConnector(http_client),
        }

        # Fallback connector for unknown ATS types
        self._fallback = GenericHTMLConnector(http_client)

    def get_connector(self, ats_type: ATSType) -> BaseConnector:
        """Get the appropriate connector for an ATS type."""
        return self._connectors.get(ats_type, self._fallback)

    def fetch_jobs(self, company: Company) -> list[Job]:
        """Fetch jobs for a company using the appropriate connector.

        Tries the company's declared ATS first, then falls back to
        generic HTML parsing if needed.
        """
        # Skip if scraping is explicitly disabled
        if not company.scraping_allowed:
            logger.info("scraping_disabled", company=company.name)
            return []

        jobs = []
        ats_type = company.ats_type

        # Try the declared ATS connector first
        if ats_type in self._connectors and ats_type != ATSType.UNKNOWN:
            connector = self._connectors[ats_type]
            try:
                jobs = connector.fetch_jobs(company)
                if jobs:
                    return jobs
            except Exception as e:
                logger.warning(
                    "connector_failed",
                    company=company.name,
                    ats_type=ats_type.value,
                    error=str(e),
                )

        # Fall back to generic HTML if we have a careers URL
        if not jobs and company.careers_url:
            try:
                jobs = self._fallback.fetch_jobs(company)
            except Exception as e:
                logger.warning(
                    "fallback_connector_failed",
                    company=company.name,
                    error=str(e),
                )

        return jobs

    def get_raw_data(self, company: Company) -> Optional[dict]:
        """Get raw data for debugging."""
        ats_type = company.ats_type
        connector = self._connectors.get(ats_type, self._fallback)
        return connector.get_raw_data(company)
