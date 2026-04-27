"""Environment-driven configuration for the Spond MCP server."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("SPOND_MCP_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    spond_username: str | None = Field(default=None)
    spond_password: SecretStr | None = Field(default=None)
    spond_club_id: str | None = Field(default=None)

    spond_mcp_read_only: bool = Field(default=True)
    spond_mcp_allow_messages: bool = Field(default=False)
    spond_mcp_allow_attendance_changes: bool = Field(default=False)

    # Disclosure policy: even read-only callers cannot dump raw upstream
    # payloads (which contain PII, contact details, full message bodies, and
    # raw financial records) unless this is explicitly enabled by the operator.
    spond_mcp_allow_raw_payloads: bool = Field(default=False)

    # Contact detail policy: members' email and phone numbers are sensitive
    # PII. They are omitted from compact summaries by default. The agent must
    # explicitly request `include_contact=true`, AND this operator opt-in
    # must be set, to receive them.
    spond_mcp_allow_contact_details: bool = Field(default=False)

    # File-export policy: writing the XLSX attendance report to a temp file
    # is local-side-effecting (it can hold PII on disk). Off by default so the
    # default deployment never persists Spond data outside memory.
    spond_mcp_allow_file_exports: bool = Field(default=False)

    spond_mcp_max_events: int = Field(default=100, ge=1, le=1000)
    spond_mcp_timezone: str = Field(default="UTC")
    spond_mcp_cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("spond_mcp_timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        # Accept any non-empty string; resolution to a real tz happens in the
        # helper. We keep validation soft so misconfiguration does not block
        # the server from starting in read-only mode.
        return v.strip() or "UTC"

    @property
    def has_credentials(self) -> bool:
        return bool(self.spond_username and self.spond_password)

    def password_value(self) -> str | None:
        if self.spond_password is None:
            return None
        return self.spond_password.get_secret_value()

    def messages_allowed(self) -> bool:
        return not self.spond_mcp_read_only and self.spond_mcp_allow_messages

    def attendance_changes_allowed(self) -> bool:
        return not self.spond_mcp_read_only and self.spond_mcp_allow_attendance_changes

    def raw_payloads_allowed(self) -> bool:
        return self.spond_mcp_allow_raw_payloads

    def contact_details_allowed(self) -> bool:
        return self.spond_mcp_allow_contact_details

    def file_exports_allowed(self) -> bool:
        return self.spond_mcp_allow_file_exports


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Load and cache the active settings."""

    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (useful for tests)."""

    load_settings.cache_clear()
