"""spond-mcp: a Model Context Protocol server for the unofficial Spond API."""

from .config import Settings, load_settings
from .errors import (
    SpondAuthError,
    SpondMcpError,
    SpondNotFoundError,
    SpondPolicyError,
    SpondUnsupportedError,
    SpondUpstreamError,
    SpondValidationError,
)

__all__ = [
    "Settings",
    "SpondAuthError",
    "SpondMcpError",
    "SpondNotFoundError",
    "SpondPolicyError",
    "SpondUnsupportedError",
    "SpondUpstreamError",
    "SpondValidationError",
    "load_settings",
]

__version__ = "0.1.0"
