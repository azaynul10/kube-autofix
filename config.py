"""
Kube-AutoFix — Central Configuration Module.

Uses pydantic-settings to load and validate all configuration from
environment variables and .env files. The KUBE_NAMESPACE is intentionally
hardcoded as a safety guardrail — the agent may ONLY operate within
the 'autofix-agent-env' namespace.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Safety constant — NOT user-configurable ──────────────────────────
KUBE_NAMESPACE: str = "autofix-agent-env"


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── OpenAI ────────────────────────────────────────────────────────
    openai_api_key: str = Field(
        ...,
        description="OpenAI API key for GPT-4o access.",
    )
    openai_model: str = Field(
        default="gpt-4o",
        description="OpenAI model identifier.",
    )

    # ── Agent behaviour ───────────────────────────────────────────────
    max_iterations: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum autonomous retry loops (hard cap: 10).",
    )
    dry_run: bool = Field(
        default=False,
        description="If True, corrected YAML is printed but never applied.",
    )

    # ── Logging ───────────────────────────────────────────────────────
    log_level: str = Field(
        default="INFO",
        description="Logging verbosity: DEBUG, INFO, WARNING, ERROR.",
    )

    # ── K8s monitoring ────────────────────────────────────────────────
    poll_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=30,
        description="Seconds between pod-status polls.",
    )
    poll_timeout_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Maximum seconds to wait for pods to become ready.",
    )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got '{v}'")
        return upper
