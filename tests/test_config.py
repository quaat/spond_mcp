"""Tests for the env-driven settings layer."""

from __future__ import annotations

from spond_mcp.config import Settings


def test_defaults_are_safe(monkeypatch):
    for var in [
        "SPOND_USERNAME",
        "SPOND_PASSWORD",
        "SPOND_MCP_READ_ONLY",
        "SPOND_MCP_ALLOW_MESSAGES",
        "SPOND_MCP_ALLOW_ATTENDANCE_CHANGES",
    ]:
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.spond_mcp_read_only is True
    assert s.spond_mcp_allow_messages is False
    assert s.spond_mcp_allow_attendance_changes is False
    assert s.messages_allowed() is False
    assert s.attendance_changes_allowed() is False
    assert s.has_credentials is False


def test_messages_require_both_flags():
    s = Settings(
        spond_username="u",
        spond_password="p",
        spond_mcp_read_only=False,
        spond_mcp_allow_messages=True,
    )
    assert s.messages_allowed() is True

    s2 = Settings(
        spond_username="u",
        spond_password="p",
        spond_mcp_read_only=True,
        spond_mcp_allow_messages=True,
    )
    assert s2.messages_allowed() is False


def test_attendance_requires_both_flags():
    s = Settings(
        spond_username="u",
        spond_password="p",
        spond_mcp_read_only=False,
        spond_mcp_allow_attendance_changes=True,
    )
    assert s.attendance_changes_allowed() is True

    s2 = Settings(
        spond_username="u",
        spond_password="p",
        spond_mcp_read_only=True,
        spond_mcp_allow_attendance_changes=True,
    )
    assert s2.attendance_changes_allowed() is False


def test_password_is_secret_string():
    s = Settings(spond_username="u", spond_password="hunter2")
    # Repr must redact the password.
    assert "hunter2" not in repr(s)
    assert s.password_value() == "hunter2"
