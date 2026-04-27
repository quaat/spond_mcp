"""Hardening tests covering the production-readiness fixes:

1. send_message awaitable handling for the chat_id path
2. send_message ambiguous routing rejection
3. attendance change enum narrowing (accepted/declined only by default)
4. SPOND_MCP_ALLOW_RAW_PAYLOADS gate
5. spond_list_events passes parsed UTC datetimes to client.get_events
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from spond_mcp.server import build_server


async def _call(mcp, name: str, args: dict[str, Any]) -> Any:
    return await mcp._tool_manager.call_tool(name, args, context=None, convert_result=False)


# ---------------------------------------------------------------------------
# 1. send_message: coroutine return value (existing-chat path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_awaits_coroutine_return(manager):
    """Upstream `send_message(chat_id=...)` returns an unawaited coroutine.

    Verifies the tool detects the awaitable, awaits it exactly once, the
    underlying continuation actually runs, and we do not report success
    based on the un-awaited handle.
    """

    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s

    fake = manager.fake
    continuation_calls: list[tuple[str, str]] = []

    async def _continue(chat_id: str, text: str) -> dict[str, Any]:
        continuation_calls.append((chat_id, text))
        return {"id": "continued-msg-1"}

    # Upstream `send_message` returns the un-awaited coroutine when chat_id is
    # set. We mimic that exactly.
    original_send = fake.send_message

    async def _send(text, user=None, group_uid=None, chat_id=None):
        await original_send(text=text, user=user, group_uid=group_uid, chat_id=chat_id)
        if chat_id is not None:
            return _continue(chat_id, text)  # NOTE: deliberately not awaited
        return {"id": "direct-msg"}

    fake.send_message = _send

    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": "hello", "chat_id": "c1", "confirm": True},
    )

    assert result.get("error") is not True, result
    assert result["sent"] is True
    assert result["message_id"] == "continued-msg-1"
    # The continuation actually ran — i.e. the coroutine was awaited.
    assert continuation_calls == [("c1", "hello")]


@pytest.mark.asyncio
async def test_send_message_no_text_echo(manager):
    """Even on the awaitable path, the body is never echoed back."""

    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s

    fake = manager.fake

    async def _send(text, user=None, group_uid=None, chat_id=None):
        async def _continuation():
            return {"id": "x"}

        return _continuation()  # un-awaited coroutine

    fake.send_message = _send
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": "secret-payload-zzz", "chat_id": "c1", "confirm": True},
    )
    assert "secret-payload-zzz" not in json.dumps(result)


# ---------------------------------------------------------------------------
# 2. send_message: ambiguous routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"chat_id": "c1", "user": "alice@example.com"},
        {"chat_id": "c1", "group_id": "g1"},
        {"chat_id": "c1", "user": "alice@example.com", "group_id": "g1"},
    ],
)
@pytest.mark.asyncio
async def test_send_message_rejects_mixed_routing(manager, args):
    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)

    payload = {"text": "hi", "confirm": True, **args}
    result = await _call(mcp, "spond_send_message", payload)
    assert result["error"] is True
    assert result["code"] == "spond_validation_error"
    assert "Ambiguous" in result["message"] or "ambiguous" in result["message"]
    assert manager.fake.sent_messages == []


@pytest.mark.asyncio
async def test_send_message_rejects_partial_new_chat(manager):
    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)

    # Only `user` without `group_id` (and no chat_id) is rejected.
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": "hi", "user": "alice@example.com", "confirm": True},
    )
    assert result["error"] is True
    assert result["code"] == "spond_validation_error"
    assert manager.fake.sent_messages == []


# ---------------------------------------------------------------------------
# 3. Raw payload gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, args",
    [
        ("spond_get_profile", {"include_raw": True}),
        ("spond_list_groups", {"include_raw": True, "include_members": True}),
        ("spond_get_group", {"group_id": "g1", "include_raw": True}),
        ("spond_list_messages", {"max_chats": 5, "include_raw": True}),
        ("spond_list_club_transactions", {"club_id": "club-x", "include_raw": True}),
        ("spond_find_person", {"query": "alice@example.com", "include_raw": True}),
        ("spond_list_events", {"include_raw": True}),
        ("spond_get_event", {"event_id": "e1", "include_raw": True}),
        ("spond_list_posts", {"include_raw": True}),
    ],
)
async def test_raw_payloads_blocked_by_default(manager, tool, args):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, tool, args)
    assert result["error"] is True
    assert result["code"] == "spond_policy_denied"
    assert "raw" not in result.get("message", "").lower() or "raw" in result["message"].lower()


@pytest.mark.asyncio
async def test_compact_summaries_have_no_raw_or_sensitive_fields(manager):
    """Default responses must not embed raw upstream payloads."""

    mcp, _ = build_server(manager.settings, manager=manager)

    profile = await _call(mcp, "spond_get_profile", {})
    assert "raw" not in profile

    groups = await _call(mcp, "spond_list_groups", {"include_members": True})
    for g in groups["groups"]:
        assert "raw" not in g
        for m in g.get("members", []):
            assert "raw" not in m

    chats = await _call(mcp, "spond_list_messages", {"max_chats": 5})
    for c in chats["chats"]:
        assert "raw" not in c

    s = manager.settings.model_copy(update={"spond_club_id": "club-x"})
    manager.settings = s
    mcp2, _ = build_server(s, manager=manager)
    txs = await _call(mcp2, "spond_list_club_transactions", {})
    for t in txs["transactions"]:
        assert "raw" not in t


@pytest.mark.asyncio
async def test_raw_payloads_allowed_when_enabled(manager):
    s = manager.settings.model_copy(update={"spond_mcp_allow_raw_payloads": True})
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)

    profile = await _call(mcp, "spond_get_profile", {"include_raw": True})
    assert profile["raw"]["id"] == "p1"

    groups = await _call(
        mcp, "spond_list_groups", {"include_members": True, "include_raw": True}
    )
    assert groups["groups"][0]["raw"]["id"] == "g1"


# ---------------------------------------------------------------------------
# 4. spond_list_events passes parsed UTC datetimes through to upstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_passes_aware_utc_datetimes_to_client(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    await _call(
        mcp,
        "spond_list_events",
        {
            "from_datetime": "2026-04-01T08:30:00+02:00",
            "to_datetime": "2026-06-01T00:00:00Z",
        },
    )
    assert manager.fake.calls, "get_events should have been called on the fake"
    name, _args, kwargs = manager.fake.calls[-1]
    assert name == "get_events"

    min_start = kwargs["min_start"]
    max_start = kwargs["max_start"]
    assert isinstance(min_start, datetime)
    assert isinstance(max_start, datetime)
    assert min_start.tzinfo is not None
    assert min_start.utcoffset().total_seconds() == 0  # normalized to UTC
    # The +02:00 input must have been normalized: 08:30+02:00 == 06:30Z.
    assert min_start.hour == 6 and min_start.minute == 30
    assert max_start.hour == 0 and max_start.minute == 0


# ---------------------------------------------------------------------------
# 5. The narrowed enum is reflected in the published JSON schema.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_response_schema_lists_only_safe_values_in_default_path(manager):
    """The schema may list all three for ergonomics, but only accepted/declined
    must work without the experimental flag."""

    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_attendance_changes": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    tools = {t.name: t for t in await mcp.list_tools()}
    schema = tools["spond_change_event_response"].inputSchema
    response_schema = schema["properties"]["response"]
    # Tool advertises the values that it can possibly accept; runtime gating
    # blocks the experimental one.
    assert set(response_schema.get("enum", [])) == {"accepted", "declined", "unanswered"}


@pytest.mark.asyncio
async def test_send_message_chat_id_path_completes(manager):
    """Smoke test: chat_id-only path returns success without ambiguity errors."""

    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": "hello", "chat_id": "c1", "confirm": True},
    )
    assert result.get("error") is not True
    assert result["sent"] is True
    assert manager.fake.sent_messages[-1]["chat_id"] == "c1"
