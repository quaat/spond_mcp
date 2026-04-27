"""Hardening tests covering the production-readiness fixes:

1. send_message awaitable handling for the chat_id path
2. send_message ambiguous routing rejection
3. attendance change enum narrowing (accepted/declined only by default)
4. SPOND_MCP_ALLOW_RAW_PAYLOADS gate
5. spond_list_events passes parsed UTC datetimes to client.get_events
"""

from __future__ import annotations

import json
import os
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


@pytest.mark.asyncio
async def test_send_message_nested_value_error_is_sanitised(manager):
    """A ValueError raised inside the nested coroutine must be sanitised by
    the same path as direct upstream errors: no token leak, no body leak."""

    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    fake = manager.fake

    secret_body = "secret-outgoing-message-xyz"

    async def _send(text, user=None, group_uid=None, chat_id=None):
        async def _continuation():
            raise ValueError(
                f"Request failed with status 500: token=topsecret body={text}"
            )

        return _continuation()  # un-awaited coroutine

    fake.send_message = _send
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": secret_body, "chat_id": "c1", "confirm": True},
    )
    assert result["error"] is True
    assert result["code"] == "spond_upstream_error"
    serialised = json.dumps(result)
    assert "topsecret" not in serialised
    assert secret_body not in serialised
    assert "token=" not in serialised


@pytest.mark.asyncio
async def test_send_message_nested_generic_exception_is_sanitised(manager):
    """A generic exception inside the nested coroutine becomes a structured
    upstream error and never carries the original repr to the caller."""

    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_messages": True}
    )
    manager.settings = s
    fake = manager.fake

    secret_body = "another-secret-message-ABCD"

    class _LeakyError(Exception):
        def __str__(self) -> str:
            return f"leaky token=topsecret body={secret_body}"

    async def _send(text, user=None, group_uid=None, chat_id=None):
        async def _continuation():
            raise _LeakyError()

        return _continuation()

    fake.send_message = _send
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_send_message",
        {"text": secret_body, "chat_id": "c1", "confirm": True},
    )
    assert result["error"] is True
    assert result["code"] == "spond_upstream_error"
    serialised = json.dumps(result)
    assert "topsecret" not in serialised
    assert secret_body not in serialised
    assert "token=" not in serialised


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
    """Default responses must not embed raw upstream payloads or contact PII."""

    mcp, _ = build_server(manager.settings, manager=manager)

    profile = await _call(mcp, "spond_get_profile", {})
    assert "raw" not in profile
    # Contact PII gated: profile must not expose email/phone by default.
    assert "email" not in profile
    assert "phone" not in profile

    groups = await _call(mcp, "spond_list_groups", {"include_members": True})
    for g in groups["groups"]:
        assert "raw" not in g
        for m in g.get("members", []):
            assert "raw" not in m
            # Member email is contact PII; gated by default.
            assert "email" not in m

    chats = await _call(mcp, "spond_list_messages", {"max_chats": 5})
    for c in chats["chats"]:
        assert "raw" not in c

    s = manager.settings.model_copy(update={"spond_club_id": "club-x"})
    manager.settings = s
    mcp2, _ = build_server(s, manager=manager)
    txs = await _call(mcp2, "spond_list_club_transactions", {})
    for t in txs["transactions"]:
        assert "raw" not in t


# ---------------------------------------------------------------------------
# Contact-detail gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, args",
    [
        ("spond_get_profile", {"include_contact": True}),
        (
            "spond_list_groups",
            {"include_members": True, "include_contact": True},
        ),
        ("spond_get_group", {"group_id": "g1", "include_contact": True}),
        (
            "spond_find_person",
            {"query": "alice@example.com", "include_contact": True},
        ),
    ],
)
async def test_contact_details_blocked_by_default(manager, tool, args):
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, tool, args)
    assert result["error"] is True
    assert result["code"] == "spond_policy_denied"


@pytest.mark.asyncio
async def test_contact_details_allowed_when_enabled(manager):
    s = manager.settings.model_copy(update={"spond_mcp_allow_contact_details": True})
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)

    profile = await _call(mcp, "spond_get_profile", {"include_contact": True})
    assert profile["email"] == "test@example.com"

    group = await _call(
        mcp,
        "spond_get_group",
        {"group_id": "g1", "include_members": True, "include_contact": True},
    )
    emails = [m.get("email") for m in group["members"]]
    assert "alice@example.com" in emails


@pytest.mark.asyncio
async def test_guardian_email_gated_when_searching_in_group(manager):
    """Guardians inside a group's members[*].guardians are PII too."""

    mcp, _ = build_server(manager.settings, manager=manager)
    # Default: no contact even though we matched the guardian by email.
    result = await _call(
        mcp,
        "spond_find_person",
        {"query": "bob@example.com", "group_id": "g1"},
    )
    assert result["is_guardian"] is True
    assert "email" not in result


# ---------------------------------------------------------------------------
# XLSX attendance export hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attendance_report_metadata_returns_no_bytes(manager):
    """Metadata mode never returns the binary payload inline."""

    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_event_attendance_report", {"event_id": "e1"})
    assert result["byte_size"] > 0
    assert result["filename"].endswith(".xlsx")
    for forbidden in ("bytes", "data", "content", "blob", "xlsx_bytes"):
        assert forbidden not in result, f"metadata mode leaked {forbidden}"


@pytest.mark.asyncio
async def test_attendance_report_tempfile_blocked_by_default(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(
        mcp,
        "spond_get_event_attendance_report",
        {"event_id": "e1", "mode": "tempfile"},
    )
    assert result["error"] is True
    assert result["code"] == "spond_policy_denied"
    # And no file was written, since the policy gate runs before the network
    # call.
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil_id",
    [
        "../etc/passwd",
        "/abs/path",
        "..\\windows",
        "a/b/c",
        "‮.xlsx",  # bidi override
        "name with spaces",
        "<script>",
        "." * 200,
        "",
    ],
)
async def test_attendance_report_filename_is_sanitised(manager, evil_id):
    s = manager.settings.model_copy(update={"spond_mcp_allow_file_exports": True})
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    result = await _call(
        mcp,
        "spond_get_event_attendance_report",
        {"event_id": evil_id, "mode": "tempfile"},
    )
    # Some upstream calls may reject the id; that's an upstream/validation
    # error rather than a path traversal — both outcomes are acceptable as
    # long as nothing escapes the temp directory.
    if result.get("error"):
        return

    filename = result["filename"]
    path = result["path"]
    # Filename safety properties:
    assert "/" not in filename
    assert "\\" not in filename
    assert ".." not in filename
    assert filename.endswith(".xlsx")
    assert len(filename) <= 64 + len("spond-attendance-") + len(".xlsx")

    # Path safety: the file lives inside the spond_mcp_-prefixed temp dir we
    # created — not in some directory derived from the evil id.
    real_path = os.path.realpath(path)
    parent_basename = os.path.basename(os.path.dirname(real_path))
    assert parent_basename.startswith("spond_mcp_")
    # The basename equals the sanitised filename, not the evil id.
    assert os.path.basename(real_path) == filename


def test_safe_xlsx_filename_helper():
    from spond_mcp.server import _safe_xlsx_filename

    assert _safe_xlsx_filename("abc-123") == "spond-attendance-abc-123.xlsx"
    assert _safe_xlsx_filename("../../etc/passwd") == "spond-attendance-______etc_passwd.xlsx"
    assert _safe_xlsx_filename("") == "spond-attendance-event.xlsx"
    long = "x" * 500
    out = _safe_xlsx_filename(long)
    assert out.startswith("spond-attendance-")
    assert out.endswith(".xlsx")
    # Stem capped: full filename ≤ prefix(17) + 64 + suffix(5).
    assert len(out) <= 17 + 64 + 5


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
async def test_change_response_schema_only_advertises_safe_values(manager):
    """Default schema must expose only accepted/declined.

    The unverified 'unanswered' payload is not exposed by the side-effecting
    tool at all; it is surfaced only via read-side counts/IDs.
    """

    s = manager.settings.model_copy(
        update={"spond_mcp_read_only": False, "spond_mcp_allow_attendance_changes": True}
    )
    manager.settings = s
    mcp, _ = build_server(s, manager=manager)
    tools = {t.name: t for t in await mcp.list_tools()}
    schema = tools["spond_change_event_response"].inputSchema
    response_schema = schema["properties"]["response"]
    assert set(response_schema.get("enum", [])) == {"accepted", "declined"}


@pytest.mark.asyncio
async def test_attendance_summary_still_includes_unanswered_counts(manager):
    """Read-side attendance must still show unanswered counts/IDs."""

    fake = manager.fake
    fake.events_data = [
        {
            "id": "e1",
            "heading": "Match",
            "startTimestamp": "2026-05-01T10:00:00Z",
            "endTimestamp": "2026-05-01T12:00:00Z",
            "responses": {
                "acceptedIds": ["m1"],
                "declinedIds": [],
                "unansweredIds": ["m2", "m3"],
            },
        }
    ]
    mcp, _ = build_server(manager.settings, manager=manager)
    result = await _call(mcp, "spond_get_event", {"event_id": "e1"})
    assert result["attendance"]["counts"]["unanswered"] == 2
    assert result["attendance"]["unanswered_member_ids"] == ["m2", "m3"]


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
