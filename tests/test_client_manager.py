"""Tests for SpondClientManager error wrapping and lifecycle."""

from __future__ import annotations

import pytest
from spond import AuthenticationError

from spond_mcp.client import SpondClientManager
from spond_mcp.errors import (
    SpondAuthError,
    SpondNotFoundError,
    SpondUpstreamError,
)


class _Sess:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self) -> None:
        self.clientsession = _Sess()


@pytest.mark.asyncio
async def test_call_wraps_authentication_error(settings_factory):
    mgr = SpondClientManager(settings_factory())

    async def _fail():
        raise AuthenticationError("nope")

    with pytest.raises(SpondAuthError):
        await mgr.call("login", _fail)


@pytest.mark.asyncio
async def test_call_wraps_value_error_without_leaking_body(settings_factory):
    mgr = SpondClientManager(settings_factory())

    async def _fail():
        raise ValueError("Request failed with status 500: token=secret")

    with pytest.raises(SpondUpstreamError) as info:
        await mgr.call("get_events", _fail)
    assert "secret" not in info.value.message


@pytest.mark.asyncio
async def test_call_wraps_key_error(settings_factory):
    mgr = SpondClientManager(settings_factory())

    async def _fail():
        raise KeyError("missing")

    with pytest.raises(SpondNotFoundError):
        await mgr.call("get_event", _fail)


@pytest.mark.asyncio
async def test_aclose_closes_sessions_and_is_idempotent(settings_factory):
    mgr = SpondClientManager(settings_factory())
    mgr._spond = _Client()  # type: ignore[assignment]
    mgr._club = _Client()  # type: ignore[assignment]
    spond_session = mgr._spond.clientsession
    club_session = mgr._club.clientsession

    await mgr.aclose()
    assert spond_session.closed is True
    assert club_session.closed is True
    # Idempotent.
    await mgr.aclose()


@pytest.mark.asyncio
async def test_get_spond_requires_credentials():
    from spond_mcp.config import Settings

    mgr = SpondClientManager(Settings(spond_username=None, spond_password=None))
    with pytest.raises(SpondAuthError):
        await mgr.get_spond()
