"""Configuration management for Role Radar."""

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TitleRule(BaseModel):
    """Composable title-matching rule.

    A rule matches a string if all `all_of` substrings are present AND at least one
    `any_of` substring is present. Empty lists are treated as "no constraint" on that
    side. A rule with both lists empty never matches (so it's a no-op).

    Example:
        # Match any "AI ... PM"-ish title that is NOT staff/principal
        include_rules:
          - any_of: ["ai pm", "ai product manager"]
          - all_of: ["product manager"]
            any_of: ["ai", "ml", "platform"]
        exclude_rules:
          - any_of: ["staff", "principal", "director"]
    """
    all_of: list[str] = Field(default_factory=list)
    any_of: list[str] = Field(default_factory=list)

    def matches(self, text: str) -> bool:
        text_lower = text.lower()
        if not self.all_of and not self.any_of:
            return False
        if self.all_of and not all(t.lower() in text_lower for t in self.all_of):
            return False
        if self.any_of and not any(t.lower() in text_lower for t in self.any_of):
            return False
        return True


class Preferences(BaseModel):
    """User preferences for job search."""
    location: str = "San Francisco Bay Area"
    include_remote: bool = True
    seniority: list[str] = Field(
        default_factory=lambda: ["PM", "Senior PM", "Staff PM", "Group PM"]
    )
    # Composable rules — preferred over flat substring lists when set.
    include_rules: list[TitleRule] = Field(default_factory=list)
    exclude_rules: list[TitleRule] = Field(default_factory=list)
    allowed_titles: list[str] = Field(
        default_factory=lambda: [
            "Product Manager",
            "PM",
            "Technical Product Manager",
            "Technical PM",
            "AI Product Manager",
            "AI PM",
            "Platform PM",
            "Platform Product Manager",
            "Senior Product Manager",
            "Sr. Product Manager",
            "Sr PM",
            "Staff Product Manager",
            "Group Product Manager",
            "Principal Product Manager",
            "Lead Product Manager",
            "Associate Product Manager",
            "APM",
        ]
    )
    excluded_keywords: list[str] = Field(
        default_factory=lambda: [
            "Sales",
            "HR",
            "Human Resources",
            "Marketing",
            "Recruiting",
            "Recruiter",
            "Account",
            "Customer Success",
            "Support",
        ]
    )
    required_keywords: list[str] = Field(default_factory=list)
    max_roles_per_email: int = 15
    boost_ai_companies: bool = True
    boost_vc_backed: bool = True

    @classmethod
    def from_yaml(cls, path: Path) -> "Preferences":
        """Load preferences from YAML file."""
        if not path.exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)


class EmailConfig(BaseModel):
    """Email configuration."""
    provider: str = "smtp"  # smtp or sendgrid
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    sendgrid_api_key: str = ""
    from_email: str = ""
    to_email: str = ""
    test_mode: bool = False


class Settings(BaseSettings):
    """Application settings from environment."""
    # Paths
    base_dir: Path = Field(default_factory=lambda: Path.cwd())
    output_dir: Path = Field(default_factory=lambda: Path.cwd() / "outputs")
    cache_dir: Path = Field(default_factory=lambda: Path.cwd() / ".cache")
    data_dir: Path = Field(default_factory=lambda: Path.cwd() / "data")

    # Database
    db_path: Path = Field(default_factory=lambda: Path.cwd() / ".cache" / "role_radar.db")

    # Email
    email_provider: str = "smtp"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    sendgrid_api_key: str = ""
    email_from: str = ""
    email_to: str = ""
    email_test_mode: bool = False

    # OpenAI (optional)
    openai_api_key: str = ""
    use_openai_for_scoring: bool = False

    # HTTP settings
    user_agent: str = "RoleRadar/1.0 (Job Search Tool; +https://github.com/roleradar)"
    request_timeout: int = 30
    rate_limit_requests_per_second: float = 2.0
    max_retries: int = 3

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json or text

    class Config:
        env_prefix = "ROLE_RADAR_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    def get_email_config(self) -> EmailConfig:
        """Get email configuration."""
        return EmailConfig(
            provider=self.email_provider,
            smtp_host=self.smtp_host,
            smtp_port=self.smtp_port,
            smtp_username=self.smtp_username,
            smtp_password=self.smtp_password,
            sendgrid_api_key=self.sendgrid_api_key,
            from_email=self.email_from,
            to_email=self.email_to,
            test_mode=self.email_test_mode,
        )

    def ensure_dirs(self) -> None:
        """Ensure all required directories exist."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Optional[Path] = None) -> Settings:
    """Load settings from environment and .env file.

    Uses ``override=True`` so values in .env take precedence over empty/stale
    shell vars (common when ANTHROPIC_API_KEY is set to "" in a parent shell).
    """
    if env_file:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv(override=True)

    return Settings()


def create_default_env_file(path: Path) -> None:
    """Create a default .env template file."""
    template = """# Role Radar Configuration
# Copy this file to .env and fill in your values

# Email settings (choose one provider)
ROLE_RADAR_EMAIL_PROVIDER=smtp  # smtp or sendgrid

# SMTP settings (for Gmail, use an App Password)
ROLE_RADAR_SMTP_HOST=smtp.gmail.com
ROLE_RADAR_SMTP_PORT=587
ROLE_RADAR_SMTP_USERNAME=your-email@gmail.com
ROLE_RADAR_SMTP_PASSWORD=your-app-password

# SendGrid settings (alternative to SMTP)
ROLE_RADAR_SENDGRID_API_KEY=your-sendgrid-api-key

# Email addresses
ROLE_RADAR_EMAIL_FROM=your-email@gmail.com
ROLE_RADAR_EMAIL_TO=your-email@gmail.com

# Test mode (set to true to print email instead of sending)
ROLE_RADAR_EMAIL_TEST_MODE=true

# OpenAI (optional, for enhanced CV parsing and scoring)
ROLE_RADAR_OPENAI_API_KEY=
ROLE_RADAR_USE_OPENAI_FOR_SCORING=false

# HTTP settings
ROLE_RADAR_REQUEST_TIMEOUT=30
ROLE_RADAR_RATE_LIMIT_REQUESTS_PER_SECOND=2.0

# Logging
ROLE_RADAR_LOG_LEVEL=INFO
ROLE_RADAR_LOG_FORMAT=json
"""
    path.write_text(template)


def create_default_preferences_file(path: Path) -> None:
    """Create a default preferences.yaml file."""
    template = """# Role Radar Preferences
# Customize your job search preferences here

# Target location for job search
location: "San Francisco Bay Area"

# Include remote positions
include_remote: true

# Target seniority levels
seniority:
  - "PM"
  - "Senior PM"
  - "Staff PM"
  - "Group PM"

# Allowed job titles (case-insensitive matching)
allowed_titles:
  - "Product Manager"
  - "PM"
  - "Technical Product Manager"
  - "Technical PM"
  - "AI Product Manager"
  - "AI PM"
  - "Platform PM"
  - "Platform Product Manager"
  - "Senior Product Manager"
  - "Sr. Product Manager"
  - "Sr PM"
  - "Staff Product Manager"
  - "Group Product Manager"
  - "Principal Product Manager"
  - "Lead Product Manager"
  - "Associate Product Manager"
  - "APM"

# Keywords to exclude from job titles/descriptions
excluded_keywords:
  - "Sales"
  - "HR"
  - "Human Resources"
  - "Marketing"
  - "Recruiting"
  - "Recruiter"
  - "Account"
  - "Customer Success"
  - "Support"

# Required keywords (job must contain at least one)
# Leave empty to not require any specific keywords
required_keywords: []

# Maximum number of roles to include in email
max_roles_per_email: 15

# Scoring boosts
boost_ai_companies: true
boost_vc_backed: true
"""
    path.write_text(template)


def create_default_portfolios_csv(path: Path) -> None:
    """Create a default portfolios.csv file."""
    template = """# VC Portfolio Companies
# Format: company_name,homepage_url,careers_url,vc_backers,notes
# You can add/override portfolio companies here

# Example entries:
# Acme AI,https://acme.ai,https://acme.ai/careers,"Sequoia, a16z",AI infrastructure startup
# TechCorp,https://techcorp.com,https://jobs.lever.co/techcorp,Greylock,Enterprise SaaS

# Add your entries below:
"""
    path.write_text(template)
