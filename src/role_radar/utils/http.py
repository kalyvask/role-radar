"""HTTP utilities with rate limiting, caching, and robots.txt checking."""

import hashlib
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from role_radar.utils.logging import get_logger

logger = get_logger(__name__)


# HTTP status codes worth retrying. 429 = rate limited, 5xx = transient server errors.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _should_retry(exc: BaseException) -> bool:
    """Retry on connection errors, timeouts, and retryable HTTP status codes."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRYABLE_STATUS_CODES
    return False


class RateLimiter:
    """Simple token bucket rate limiter."""

    def __init__(self, requests_per_second: float = 2.0):
        self.requests_per_second = requests_per_second
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time: dict[str, float] = {}

    def wait(self, domain: str) -> None:
        """Wait if necessary to respect rate limit for a domain."""
        now = time.time()
        last_time = self.last_request_time.get(domain, 0)
        elapsed = now - last_time

        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            logger.debug("rate_limiting", domain=domain, sleep_seconds=sleep_time)
            time.sleep(sleep_time)

        self.last_request_time[domain] = time.time()


class RobotsChecker:
    """Check robots.txt for crawling permissions."""

    def __init__(self, user_agent: str, cache_dir: Path):
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self._parsers: dict[str, Optional[RobotFileParser]] = {}

    def _get_parser(self, base_url: str) -> Optional[RobotFileParser]:
        """Get or create a robots.txt parser for a domain."""
        parsed = urlparse(base_url)
        domain = f"{parsed.scheme}://{parsed.netloc}"

        if domain in self._parsers:
            return self._parsers[domain]

        robots_url = urljoin(domain, "/robots.txt")
        parser = RobotFileParser()

        try:
            parser.set_url(robots_url)
            parser.read()
            self._parsers[domain] = parser
            logger.debug("robots_loaded", domain=domain)
        except Exception as e:
            logger.warning("robots_fetch_failed", domain=domain, error=str(e))
            self._parsers[domain] = None

        return self._parsers[domain]

    def can_fetch(self, url: str) -> bool:
        """Check if the URL can be fetched according to robots.txt."""
        parser = self._get_parser(url)
        if parser is None:
            # If we can't get robots.txt, be conservative
            return True

        can = parser.can_fetch(self.user_agent, url)
        if not can:
            logger.info("robots_disallowed", url=url)
        return can


class HTTPClient:
    """HTTP client with rate limiting, retries, and robots checking."""

    def __init__(
        self,
        user_agent: str,
        timeout: int = 30,
        requests_per_second: float = 2.0,
        cache_dir: Optional[Path] = None,
        check_robots: bool = True,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.rate_limiter = RateLimiter(requests_per_second)
        self.check_robots = check_robots
        self.cache_dir = cache_dir or Path(".cache/http")

        self.robots_checker = RobotsChecker(user_agent, self.cache_dir)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        parsed = urlparse(url)
        return parsed.netloc

    def _cache_key(self, url: str) -> str:
        """Generate cache key for URL."""
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_should_retry),
        reraise=True,
    )
    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        skip_robots_check: bool = False,
    ) -> requests.Response:
        """Make a GET request with rate limiting and retries.

        Retries up to 3 times on connection errors, timeouts, and 429/5xx HTTP responses
        with exponential backoff (2s → 30s). 429 responses are logged explicitly so
        the user knows to lower `rate_limit_requests_per_second` in settings.
        """
        domain = self._get_domain(url)

        # Check robots.txt
        if self.check_robots and not skip_robots_check:
            if not self.robots_checker.can_fetch(url):
                raise ValueError(f"Robots.txt disallows fetching: {url}")

        # Rate limit
        self.rate_limiter.wait(domain)

        # Make request
        merged_headers = dict(self.session.headers)
        if headers:
            merged_headers.update(headers)

        logger.debug("http_request", method="GET", url=url)

        response = self.session.get(
            url,
            params=params,
            headers=merged_headers,
            timeout=self.timeout,
        )

        logger.debug(
            "http_response",
            url=url,
            status_code=response.status_code,
            content_length=len(response.content),
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            logger.warning(
                "http_rate_limited",
                url=url,
                domain=domain,
                retry_after=retry_after,
                hint="Lower ROLE_RADAR_RATE_LIMIT_REQUESTS_PER_SECOND if this recurs.",
            )

        response.raise_for_status()
        return response

    def get_json(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        skip_robots_check: bool = False,
    ) -> Any:
        """Make a GET request and parse JSON response."""
        json_headers = {"Accept": "application/json"}
        if headers:
            json_headers.update(headers)

        response = self.get(url, params=params, headers=json_headers, skip_robots_check=skip_robots_check)
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_should_retry),
        reraise=True,
    )
    def post_json(
        self,
        url: str,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
        skip_robots_check: bool = False,
    ) -> Any:
        """Make a POST request with a JSON body and parse the JSON response.

        Used by ATS connectors (e.g. Workday) that expose POST-only search APIs.
        Mirrors `get`'s rate limiting, retries, and 429 logging.
        """
        domain = self._get_domain(url)

        if self.check_robots and not skip_robots_check:
            if not self.robots_checker.can_fetch(url):
                raise ValueError(f"Robots.txt disallows fetching: {url}")

        self.rate_limiter.wait(domain)

        merged_headers = dict(self.session.headers)
        merged_headers["Accept"] = "application/json"
        merged_headers["Content-Type"] = "application/json"
        if headers:
            merged_headers.update(headers)

        logger.debug("http_request", method="POST", url=url)

        response = self.session.post(
            url,
            json=json_body or {},
            headers=merged_headers,
            timeout=self.timeout,
        )

        logger.debug(
            "http_response",
            url=url,
            status_code=response.status_code,
            content_length=len(response.content),
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            logger.warning(
                "http_rate_limited",
                url=url,
                domain=domain,
                retry_after=retry_after,
                hint="Lower ROLE_RADAR_RATE_LIMIT_REQUESTS_PER_SECOND if this recurs.",
            )

        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        """Close the HTTP session."""
        self.session.close()
