"""End-to-end tests for the FastMCP tools using fake Spond clients."""

from __future__ import annotations

from typing import Any

import pytest

from spond_mcp.server import build_server


async def _call(mcp, name: str, args: dict[str, Any]) -> Any:
    return await mcp._tool_manager.call_tool(name, args, context=None, convert_result=False)


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_profile_returns_summary(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_profile", {})
    assert result["full_name"] == "Test User"
    assert result["email"] == "test@example.com"
    assert "raw" not in result


@pytest.mark.asyncio
async def test_get_profile_with_raw_includes_raw(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_profile", {"include_raw": True})
    assert result["raw"]["id"] == "p1"


@pytest.mark.asyncio
async def test_list_groups(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_list_groups", {})
    assert result["count"] == 1
    assert result["groups"][0]["group_id"] == "g1"
    assert "members" not in result["groups"][0]


@pytest.mark.asyncio
async def test_get_group_with_members(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_group", {"group_id": "g1"})
    assert result["name"] == "Team"
    assert result["members"][0]["full_name"] == "Alice A"


@pytest.mark.asyncio
async def test_get_group_unknown_returns_error(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_group", {"group_id": "missing"})
    assert result["error"] is True
    assert result["code"] == "spond_not_found"


@pytest.mark.asyncio
async def test_find_person_by_email(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_find_person", {"query": "alice@example.com"})
    assert result["full_name"] == "Alice A"


@pytest.mark.asyncio
async def test_find_person_finds_guardian_in_group(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp,
        "spond_find_person",
        {"query": "bob@example.com", "group_id": "g1"},
    )
    assert result["is_guardian"] is True


@pytest.mark.asyncio
async def test_list_events_parses_iso_datetimes(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp,
        "spond_list_events",
        {"from_datetime": "2026-04-01T00:00:00Z", "to_datetime": "2026-06-01T00:00:00Z"},
    )
    assert result["count"] == 1
    assert result["events"][0]["start"].startswith("2026-05-01T10:00:00")

    args = manager.fake.calls[0][2]
    assert args["min_start"] is not None
    assert args["max_start"] is not None


@pytest.mark.asyncio
async def test_list_events_rejects_bad_datetime(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_list_events", {"from_datetime": "not-a-date"})
    assert result["error"] is True
    assert result["code"] == "spond_validation_error"


@pytest.mark.asyncio
async def test_list_events_caps_max_events(manager):
    s = manager.settings.model_copy(update={"spond_mcp_max_events": 5})
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    result = await _call(mcp, "spond_list_events", {"max_events": 1000})
    assert result["max_events_applied"] == 5


@pytest.mark.asyncio
async def test_summarize_schedule_groups_by_day(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp,
        "spond_summarize_schedule",
        {
            "from_datetime": "2026-04-01T00:00:00Z",
            "to_datetime": "2026-06-01T00:00:00Z",
        },
    )
    assert result["total_events"] == 1
    assert result["days"][0]["date"] == "2026-05-01"


@pytest.mark.asyncio
async def test_get_event_with_attendance(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_event", {"event_id": "e1"})
    assert result["heading"] == "Match"
    assert result["attendance"]["counts"]["accepted"] == 1


@pytest.mark.asyncio
async def test_attendance_report_metadata_does_not_leak_bytes(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_event_attendance_report", {"event_id": "e1"})
    assert result["byte_size"] > 0
    assert result["filename"].endswith(".xlsx")
    assert "bytes" not in result
    assert "data" not in result


@pytest.mark.asyncio
async def test_attendance_report_tempfile_writes_file(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp,
        "spond_get_event_attendance_report",
        {"event_id": "e1", "mode": "tempfile"},
    )
    assert "path" in result
    with open(result["path"], "rb") as fh:
        assert fh.read(4) == b"PK\x03\x04"


@pytest.mark.asyncio
async def test_list_messages_truncates_text(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_list_messages", {"max_chats": 10})
    chat = result["chats"][0]
    assert chat["text_preview"] is not None
    # text_preview is truncated to 160 chars + ellipsis at most.
    assert len(chat["text_preview"]) <= 161
    assert "raw" not in chat


@pytest.mark.asyncio
async def test_list_messages_with_raw(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp, "spond_list_messages", {"max_chats": 10, "include_raw": True}
    )
    assert "raw" in result["chats"][0]


# ---------------------------------------------------------------------------
# Side-effect gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_blocked_in_read_only(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": "hello", "chat_id": "c1", "confirm": True},
    )
    assert result["error"] is True
    assert result["code"] == "spond_policy_denied"
    assert manager.fake.sent_messages == []


@pytest.mark.asyncio
async def test_send_message_requires_confirm(manager):
    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": "hello", "chat_id": "c1", "confirm": False},
    )
    assert result["error"] is True
    assert manager.fake.sent_messages == []


@pytest.mark.asyncio
async def test_send_message_validates_inputs(manager):
    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)

    # Empty text rejected.
    result = await _call(
        mcp, "spond_send_message", {"text": " ", "chat_id": "c1", "confirm": True}
    )
    assert result["error"] is True

    # Missing chat_id and missing user/group rejected.
    result = await _call(mcp, "spond_send_message", {"text": "hi", "confirm": True})
    assert result["error"] is True


@pytest.mark.asyncio
async def test_send_message_does_not_echo_text(manager):
    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": "secret-payload", "chat_id": "c1", "confirm": True},
    )
    assert result["sent"] is True
    assert "text" not in result
    # Sanity: the text never appears in the response envelope.
    import json

    assert "secret-payload" not in json.dumps(result)


@pytest.mark.asyncio
async def test_change_response_blocked_in_read_only(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp,
        "spond_change_event_response",
        {"event_id": "e1", "user_id": "m1", "response": "accepted", "confirm": True},
    )
    assert result["error"] is True
    assert manager.fake.change_response_payloads == []


@pytest.mark.asyncio
async def test_change_response_requires_confirm(manager):
    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_attendance_changes": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_change_event_response",
        {"event_id": "e1", "user_id": "m1", "response": "accepted", "confirm": False},
    )
    assert result["error"] is True
    assert manager.fake.change_response_payloads == []


@pytest.mark.asyncio
async def test_change_response_translates_enum(manager):
    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_attendance_changes": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)

    for label, expected in (
        ("accepted", {"accepted": "true"}),
        ("declined", {"accepted": "false"}),
        ("unanswered", {"accepted": "unanswered"}),
    ):
        result = await _call(
            mcp,
            "spond_change_event_response",
            {"event_id": "e1", "user_id": "m1", "response": label, "confirm": True},
        )
        assert "error" not in result or result.get("error") is None or result.get("error") is False
        assert manager.fake.change_response_payloads[-1] == expected


# ---------------------------------------------------------------------------
# Posts and transactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_posts_returns_summaries(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_list_posts", {"max_posts": 10})
    assert result["count"] == 1
    assert result["posts"][0]["post_id"] == "post1"


@pytest.mark.asyncio
async def test_list_posts_unsupported_when_method_missing(monkeypatch):
    from spond_mcp.client import SpondClientManager
    from tests.conftest import FakeSpond, make_settings

    settings = make_settings()
    mgr = SpondClientManager(settings)
    fake = FakeSpond(has_get_posts=False)

    async def _get_spond():
        return fake

    monkeypatch.setattr(mgr, "get_spond", _get_spond)
    mcp, _ = build_server(settings, manager=mgr)
    result = await _call(mcp, "spond_list_posts", {"max_posts": 10})
    assert result["error"] is True
    assert result["code"] == "spond_unsupported"
    await mgr.aclose()


@pytest.mark.asyncio
async def test_list_club_transactions_requires_club_id(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_list_club_transactions", {})
    assert result["error"] is True
    assert result["code"] == "spond_validation_error"


@pytest.mark.asyncio
async def test_list_club_transactions_uses_env_default(manager):
    s = manager.settings.model_copy(update={"spond_club_id": "club-x"})
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    result = await _call(mcp, "spond_list_club_transactions", {})
    assert result["count"] == 1
    assert result["club_id"] == "club-x"
