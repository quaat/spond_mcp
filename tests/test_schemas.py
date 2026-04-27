"""Tests for schema mappers and time helpers."""

from __future__ import annotations

import pytest

from spond_mcp.schemas import (
    map_chat,
    map_event_detail,
    map_event_summary,
    map_group,
    map_post,
    map_profile,
    map_transaction,
    normalize_iso,
    parse_iso_datetime,
)


def test_parse_iso_datetime_handles_z_and_offsets():
    a = parse_iso_datetime("2026-05-01T10:00:00Z")
    b = parse_iso_datetime("2026-05-01T12:00:00+02:00")
    assert a.isoformat() == "2026-05-01T10:00:00+00:00"
    assert b.isoformat() == "2026-05-01T10:00:00+00:00"


def test_parse_iso_datetime_rejects_garbage():
    with pytest.raises(ValueError):
        parse_iso_datetime("not-a-date")


def test_parse_iso_datetime_returns_none_for_blank():
    assert parse_iso_datetime(None) is None
    assert parse_iso_datetime("") is None


def test_normalize_iso_with_epoch_millis():
    out = normalize_iso(1714550400000)
    assert out.startswith("2024-05-01")


def test_map_profile_redacts_when_include_raw_false():
    profile = {
        "id": "p1",
        "firstName": "A",
        "lastName": "B",
        "email": "a@b.com",
        "phoneNumber": "+1-555-0100",
        "secret": "do-not-leak",
    }
    # Default: contact stripped.
    summary = map_profile(profile).model_dump(exclude_none=True)
    assert "raw" not in summary
    assert summary["full_name"] == "A B"
    assert "email" not in summary
    assert "phone" not in summary
    # Opt-in: contact present.
    contact = map_profile(profile, include_contact=True).model_dump(exclude_none=True)
    assert contact["email"] == "a@b.com"
    assert contact["phone"] == "+1-555-0100"


def test_map_event_summary_truncates_via_detail():
    event = {
        "id": "e1",
        "heading": "Match",
        "description": "x" * 5000,
        "startTimestamp": "2026-05-01T10:00:00Z",
        "endTimestamp": "2026-05-01T12:00:00Z",
        "location": {"feature": "Stadium"},
        "responses": {"acceptedIds": ["a"], "declinedIds": [], "unansweredIds": ["b", "c"]},
    }
    summary = map_event_summary(event).model_dump(exclude_none=True)
    assert summary["event_id"] == "e1"
    assert summary["start"].startswith("2026-05-01T10:00:00")
    assert summary["response_counts"]["accepted"] == 1
    assert summary["response_counts"]["unanswered"] == 2

    detail = map_event_detail(event).model_dump(exclude_none=True)
    assert len(detail["description"]) <= 1000  # truncated


def test_map_chat_truncates_message_preview():
    chat = {"id": "c", "lastMessage": {"text": "z" * 1000, "timestamp": "2026-04-25T00:00:00Z"}}
    summary = map_chat(chat).model_dump(exclude_none=True)
    assert summary["text_preview"] is not None
    assert len(summary["text_preview"]) <= 161  # 160 + ellipsis


def test_map_post_includes_preview_and_count():
    post = {
        "id": "p1",
        "title": "Hi",
        "body": "y" * 500,
        "createdTime": "2026-04-20T09:00:00Z",
        "comments": [{}, {}],
    }
    summary = map_post(post).model_dump(exclude_none=True)
    assert summary["comment_count"] == 2
    assert len(summary["text_preview"]) <= 281


def test_map_group_member_count_without_loading_members():
    group = {
        "id": "g1",
        "name": "Team",
        "members": [{"id": "m1", "firstName": "A", "lastName": "B"}],
        "subGroups": [{"id": "s1"}],
    }
    summary = map_group(group, include_members=False).model_dump(exclude_none=True)
    assert summary["member_count"] == 1
    assert summary["subgroup_count"] == 1
    assert "members" not in summary


def test_map_transaction():
    summary = map_transaction(
        {"id": "t1", "amount": 12.5, "currency": "USD", "createdAt": "2026-01-02T00:00:00Z"}
    ).model_dump(exclude_none=True)
    assert summary["amount"] == 12.5
    assert summary["currency"] == "USD"
