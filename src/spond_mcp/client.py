"""Client manager that owns the lifecycle of the Spond and SpondClub clients.

The manager creates the underlying ``aiohttp`` session lazily on first use, so
the MCP server can boot without credentials being configured. All upstream
exceptions are normalised to :mod:`spond_mcp.errors` so the tool layer never
needs to handle raw aiohttp/library errors.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, TypeVar

from spond import AuthenticationError
from spond import club as spond_club_module
from spond import spond as spond_module

from .config import Settings
from .errors import (
    SpondAuthError,
    SpondMcpError,
    SpondUnsupportedError,
    SpondUpstreamError,
)

logger = logging.getLogger("spond_mcp.client")

T = TypeVar("T")


class _Cache:
    """Tiny TTL cache used for low-risk read calls."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = max(0, int(ttl_seconds))
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[T]], *, refresh: bool = False) -> T:
        if self.ttl == 0 or refresh:
            value = await factory()
            self._store[key] = (time.monotonic() + self.ttl, value)
            return value
        async with self._lock:
            entry = self._store.get(key)
            now = time.monotonic()
            if entry and entry[0] > now:
                return entry[1]
        value = await factory()
        self._store[key] = (time.monotonic() + self.ttl, value)
        return value

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._store.clear()
        else:
            self._store.pop(key, None)


class SpondClientManager:
    """Owns the lazy lifecycle of upstream Spond clients."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._spond: spond_module.Spond | None = None
        self._club: spond_club_module.SpondClub | None = None
        self._spond_lock = asyncio.Lock()
        self._club_lock = asyncio.Lock()
        self.cache = _Cache(settings.spond_mcp_cache_ttl_seconds)

    # ------------------------------------------------------------------ core

    def _require_credentials(self) -> tuple[str, str]:
        if not self.settings.has_credentials:
            raise SpondAuthError(
                "Spond credentials are not configured. "
                "Set SPOND_USERNAME and SPOND_PASSWORD in your environment."
            )
        return self.settings.spond_username, self.settings.password_value()  # type: ignore[return-value]

    async def get_spond(self) -> spond_module.Spond:
        if self._spond is not None:
            return self._spond
        async with self._spond_lock:
            if self._spond is None:
                username, password = self._require_credentials()
                client = spond_module.Spond(username=username, password=password)
                self._spond = client
        return self._spond

    async def get_club(self) -> spond_club_module.SpondClub:
        if self._club is not None:
            return self._club
        async with self._club_lock:
            if self._club is None:
                username, password = self._require_credentials()
                client = spond_club_module.SpondClub(username=username, password=password)
                self._club = client
        return self._club

    async def aclose(self) -> None:
        """Close all underlying aiohttp sessions. Idempotent."""

        for attr in ("_spond", "_club"):
            client = getattr(self, attr)
            if client is None:
                continue
            session = getattr(client, "clientsession", None)
            if session is not None and not session.closed:
                with suppress(Exception):
                    await session.close()
            setattr(self, attr, None)
        self.cache.invalidate()

    # ------------------------------------------------------------ wrapping

    async def call(self, op: str, coro_factory: Callable[[], Awaitable[T]]) -> T:
        """Run an upstream call, mapping common exceptions to MCP errors.

        ``op`` is a short label for diagnostics (never the credentials).
        """

        try:
            return await coro_factory()
        except SpondMcpError:
            raise
        except AuthenticationError as exc:
            # Reset the client so a future call can retry with fresh state.
            await self.aclose()
            raise SpondAuthError("Spond authentication failed.") from exc
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise self._normalize(op, exc) from exc

    async def resolve_awaitable_result(self, op: str, result: Any) -> Any:
        """Drain awaitable results returned by upstream calls.

        Some upstream methods (e.g. ``Spond.send_message`` on the ``chat_id``
        branch) return an *un-awaited* coroutine instead of awaiting it
        themselves. Repeatedly await results until the value is no longer an
        awaitable, applying the same exception sanitization as :meth:`call`
        so the nested step cannot leak raw upstream error bodies, tokens,
        cookies, or request payloads.
        """

        steps = 0
        while inspect.isawaitable(result):
            steps += 1
            if steps > 4:
                # Refuse to spin if upstream chains awaitables indefinitely.
                # Returning a clean upstream error is better than hanging.
                raise SpondUpstreamError(
                    f"Spond call '{op}' returned an unbounded chain of awaitables.",
                )
            awaitable = result
            try:
                result = await awaitable
            except SpondMcpError:
                raise
            except AuthenticationError as exc:
                await self.aclose()
                raise SpondAuthError("Spond authentication failed.") from exc
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                raise self._normalize(op, exc) from exc
        return result

    def _normalize(self, op: str, exc: Exception) -> SpondMcpError:
        """Translate a non-auth upstream exception into a sanitised MCP error.

        Centralised so :meth:`call` and :meth:`resolve_awaitable_result`
        return the *same* envelope. Raw exception bodies (which may include
        request payloads) never reach the caller verbatim.
        """

        from .errors import SpondNotFoundError

        if isinstance(exc, KeyError):
            return SpondNotFoundError(str(exc))
        if isinstance(exc, NotImplementedError):
            return SpondUnsupportedError(str(exc))
        if isinstance(exc, ValueError):
            # Upstream raises ValueError on non-2xx responses, embedding
            # body text. Strip after the first ":" so we do not leak request
            # artefacts (token=..., outgoing message text, etc.).
            msg = str(exc).split(":", 1)[0].strip() or "Spond request failed"
            logger.warning("spond.%s upstream error", op)
            return SpondUpstreamError(msg)
        logger.warning("spond.%s unexpected error: %s", op, exc.__class__.__name__)
        return SpondUpstreamError(
            f"Spond call '{op}' failed: {exc.__class__.__name__}"
        )
