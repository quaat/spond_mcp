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


@pytest.mark.asyncio
async def test_resolve_awaitable_passes_through_non_awaitable(settings_factory):
    mgr = SpondClientManager(settings_factory())
    assert await mgr.resolve_awaitable_result("op", {"id": "x"}) == {"id": "x"}
    assert await mgr.resolve_awaitable_result("op", 42) == 42
    assert await mgr.resolve_awaitable_result("op", None) is None


@pytest.mark.asyncio
async def test_resolve_awaitable_drains_one_layer(settings_factory):
    mgr = SpondClientManager(settings_factory())

    async def _inner():
        return {"id": "drained"}

    out = await mgr.resolve_awaitable_result("op", _inner())
    assert out == {"id": "drained"}


@pytest.mark.asyncio
async def test_resolve_awaitable_sanitises_value_error(settings_factory):
    mgr = SpondClientManager(settings_factory())

    async def _inner():
        raise ValueError("Request failed with status 500: token=secret body=xyz")

    with pytest.raises(SpondUpstreamError) as info:
        await mgr.resolve_awaitable_result("op", _inner())
    assert "secret" not in info.value.message
    assert "xyz" not in info.value.message
    assert "token=" not in info.value.message


@pytest.mark.asyncio
async def test_resolve_awaitable_wraps_generic_exception(settings_factory):
    mgr = SpondClientManager(settings_factory())

    class _BoomError(Exception):
        def __str__(self) -> str:  # pragma: no cover - defensive
            return "should-not-leak"

    async def _inner():
        raise _BoomError()

    with pytest.raises(SpondUpstreamError) as info:
        await mgr.resolve_awaitable_result("op", _inner())
    assert "should-not-leak" not in info.value.message


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_resolve_awaitable_caps_chain_length(settings_factory):
    """The drain loop must refuse to unwind an unbounded awaitable chain."""

    mgr = SpondClientManager(settings_factory())

    class _ForeverAwaitable:
        """Awaitable that, once awaited, returns another instance of itself.

        This makes an unbounded chain without spawning real coroutines, so the
        cap test does not depend on un-awaited coroutine cleanup behaviour.
        """

        def __await__(self):
            # Returning a generator that yields nothing and produces a new
            # awaitable as its final value lets `await` resolve to another
            # awaitable instance.
            def _gen():
                if False:
                    yield  # pragma: no cover
                return _ForeverAwaitable()

            return _gen()

    with pytest.raises(SpondUpstreamError) as info:
        await mgr.resolve_awaitable_result("op", _ForeverAwaitable())
    assert "unbounded" in info.value.message.lower()
