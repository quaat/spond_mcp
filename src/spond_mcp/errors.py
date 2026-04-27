"""Structured errors for the Spond MCP server.

All user-facing errors derive from :class:`SpondMcpError` so the MCP server can
convert them into a uniform JSON envelope. Error messages must never include
credentials, auth tokens, cookies, or message bodies.
"""

from __future__ import annotations

from typing import Any


class SpondMcpError(Exception):
    """Base class for all Spond MCP errors."""

    code: str = "spond_mcp_error"
    """Stable machine-readable error code, surfaced to the caller."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class SpondAuthError(SpondMcpError):
    """Authentication with Spond failed or no credentials are configured."""

    code = "spond_auth_error"


class SpondPolicyError(SpondMcpError):
    """A side-effecting tool was called but the active policy disallows it."""

    code = "spond_policy_denied"


class SpondValidationError(SpondMcpError):
    """A tool argument failed validation."""

    code = "spond_validation_error"


class SpondNotFoundError(SpondMcpError):
    """A referenced resource (event, group, person) was not found."""

    code = "spond_not_found"


class SpondUpstreamError(SpondMcpError):
    """The upstream Spond API returned an error or unexpected response."""

    code = "spond_upstream_error"


class SpondUnsupportedError(SpondMcpError):
    """The installed `spond` library does not expose the requested capability."""

    code = "spond_unsupported"
