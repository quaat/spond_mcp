"""Test fixtures for spond-mcp.

The fixtures here build a fully-fake Spond client so tests never touch the
network. The ``manager`` fixture returns a :class:`SpondClientManager` whose
``get_spond`` and ``get_club`` are pre-bound to the fakes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spond_mcp.client import SpondClientManager  # noqa: E402
from spond_mcp.config import Settings  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeSpond:
    """In-memory stand-in for spond.spond.Spond."""

    def __init__(
        self,
        *,
        profile: dict[str, Any] | None = None,
        groups: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        posts: list[dict[str, Any]] | None = None,
        xlsx: bytes = b"PK\x03\x04fake",
        has_get_posts: bool = True,
    ) -> None:
        self.profile_data = profile or {
            "id": "p1",
            "firstName": "Test",
            "lastName": "User",
            "email": "test@example.com",
        }
        self.groups_data = groups or [
            {
                "id": "g1",
                "name": "Team",
                "members": [
                    {
                        "id": "m1",
                        "firstName": "Alice",
                        "lastName": "A",
                        "email": "alice@example.com",
                        "profile": {"id": "p-alice"},
                        "guardians": [
                            {
                                "id": "guardian-1",
                                "firstName": "Bob",
                                "lastName": "B",
                                "email": "bob@example.com",
                                "profile": {"id": "p-bob"},
                            }
                        ],
                    }
                ],
                "subGroups": [],
            }
        ]
        self.events_data = events or [
            {
                "id": "e1",
                "heading": "Match",
                "startTimestamp": "2026-05-01T10:00:00Z",
                "endTimestamp": "2026-05-01T12:00:00Z",
                "location": {"feature": "Stadium", "address": "1 Park"},
                "responses": {
                    "acceptedIds": ["m1"],
                    "declinedIds": [],
                    "unansweredIds": [],
                },
                "owners": {"group": {"id": "g1", "name": "Team"}},
            }
        ]
        self.messages_data = messages or [
            {
                "id": "c1",
                "name": "Team chat",
                "lastMessage": {"text": "x" * 500, "timestamp": "2026-04-25T08:00:00Z"},
            }
        ]
        self.posts_data = posts or [
            {
                "id": "post1",
                "title": "Welcome",
                "body": "y" * 500,
                "createdTime": "2026-04-20T09:00:00Z",
            }
        ]
        self.xlsx_bytes = xlsx
        self.clientsession = FakeSession()
        self.calls: list[tuple[str, tuple, dict]] = []
        self.change_response_payloads: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.send_message_result: Any = {"id": "msg1"}
        if not has_get_posts:
            del self.__class__.get_posts  # type: ignore[attr-defined]

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    async def get_profile(self) -> dict[str, Any]:
        self._record("get_profile")
        return self.profile_data

    async def get_groups(self) -> list[dict[str, Any]]:
        self._record("get_groups")
        return self.groups_data

    async def get_group(self, uid: str) -> dict[str, Any]:
        self._record("get_group", uid)
        for g in self.groups_data:
            if g["id"] == uid:
                return g
        raise KeyError(f"No group {uid}")

    async def get_person(self, user: str) -> dict[str, Any]:
        self._record("get_person", user)
        for g in self.groups_data:
            for m in g["members"]:
                if m["id"] == user or m.get("email") == user:
                    return m
        raise KeyError(user)

    async def get_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("get_events", **kwargs)
        return list(self.events_data)

    async def get_event(self, uid: str) -> dict[str, Any]:
        self._record("get_event", uid)
        for e in self.events_data:
            if e["id"] == uid:
                return e
        raise KeyError(uid)

    async def get_messages(self, max_chats: int = 100) -> list[dict[str, Any]]:
        self._record("get_messages", max_chats=max_chats)
        return self.messages_data[:max_chats]

    async def get_event_attendance_xlsx(self, uid: str) -> bytes:
        self._record("get_event_attendance_xlsx", uid)
        return self.xlsx_bytes

    async def change_response(self, uid: str, user: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._record("change_response", uid, user, payload)
        self.change_response_payloads.append(payload)
        return {"acceptedIds": [user]}

    async def send_message(
        self,
        text: str,
        user: str | None = None,
        group_uid: str | None = None,
        chat_id: str | None = None,
    ) -> Any:
        self._record("send_message", text=text, user=user, group_uid=group_uid, chat_id=chat_id)
        self.sent_messages.append(
            {"text": text, "user": user, "group_uid": group_uid, "chat_id": chat_id}
        )
        return self.send_message_result

    async def get_posts(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("get_posts", **kwargs)
        return list(self.posts_data)


class FakeSpondClub:
    def __init__(self, transactions: list[dict[str, Any]] | None = None) -> None:
        self.transactions_data = transactions or [
            {
                "id": "t1",
                "amount": 100.0,
                "currency": "USD",
                "description": "Membership",
                "createdAt": "2026-01-01T00:00:00Z",
                "status": "completed",
            }
        ]
        self.clientsession = FakeSession()

    async def get_transactions(
        self, club_id: str, max_items: int = 100, skip: int | None = None
    ) -> list[dict[str, Any]]:
        return list(self.transactions_data[:max_items])


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "spond_username": "user@example.com",
        "spond_password": "secret",
        "spond_mcp_read_only": True,
        "spond_mcp_allow_messages": False,
        "spond_mcp_allow_attendance_changes": False,
        "spond_mcp_max_events": 100,
        "spond_mcp_cache_ttl_seconds": 0,
    }
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def manager(monkeypatch):
    settings = make_settings()
    mgr = SpondClientManager(settings)
    fake = FakeSpond()
    fake_club = FakeSpondClub()

    async def _get_spond():
        return fake

    async def _get_club():
        return fake_club

    monkeypatch.setattr(mgr, "get_spond", _get_spond)
    monkeypatch.setattr(mgr, "get_club", _get_club)
    mgr.fake = fake  # type: ignore[attr-defined]
    mgr.fake_club = fake_club  # type: ignore[attr-defined]
    yield mgr
    await mgr.aclose()


@pytest.fixture
def settings_factory():
    return make_settings


@pytest.fixture(autouse=True)
def _silence_loop_warnings():
    # Pytest sometimes warns about closed loops on teardown when fakes are used.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())
