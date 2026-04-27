"""Pydantic schemas and output mappers used by the MCP tools.

The mappers translate the loosely-typed JSON returned by the unofficial Spond
API into compact, agent-friendly summaries. Mappers are tolerant of missing
fields because the upstream payloads are unstable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ResponseLiteral = Literal["accepted", "declined"]
"""Allowed values for the `spond_change_event_response` tool.

Only "accepted" and "declined" are accepted by default because those are the
only payload shapes that have been verified against the upstream library
(`{"accepted": "true"}` / `{"accepted": "false"}`). The "unanswered" payload is
not documented upstream; clients can opt in to it via
`SPOND_MCP_ALLOW_EXPERIMENTAL_ATTENDANCE_PAYLOADS=true`.

Note: "unanswered" is still surfaced in *read-side* counts/IDs because it is
a documented attribute of the upstream `responses` object.
"""

ExperimentalResponseLiteral = Literal["accepted", "declined", "unanswered"]
"""Same as :data:`ResponseLiteral` but includes the experimental "unanswered"
payload. Only used when the operator has opted in."""

JSONDict = dict[str, Any]


def _strict() -> ConfigDict:
    return ConfigDict(extra="ignore", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string into an aware UTC datetime.

    Accepts either ``Z`` suffix or ``+HH:MM`` offset. Naive datetimes are
    interpreted as UTC.
    """

    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:  # pragma: no cover - exercised via tool layer
        raise ValueError(f"Invalid ISO datetime: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_iso(value: Any) -> str | None:
    """Convert a Spond timestamp (string or millis) into ISO-8601 UTC."""

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (int, float)):
        # Heuristic: treat large numbers as ms since epoch.
        seconds = value / 1000 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    if isinstance(value, str):
        try:
            return parse_iso_datetime(value).isoformat()  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            return value
    return None


def _truncate(text: str | None, limit: int = 160) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ToolError(BaseModel):
    """Uniform error envelope returned by tools."""

    model_config = _strict()

    error: bool = True
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ProfileSummary(BaseModel):
    model_config = _strict()

    profile_id: str | None = None
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    locale: str | None = None
    raw: JSONDict | None = None


class MemberSummary(BaseModel):
    model_config = _strict()

    member_id: str | None = None
    profile_id: str | None = None
    full_name: str | None = None
    email: str | None = None
    role: str | None = None
    is_guardian: bool = False
    raw: JSONDict | None = None


class GroupSummary(BaseModel):
    model_config = _strict()

    group_id: str
    name: str | None = None
    description: str | None = None
    member_count: int | None = None
    subgroup_count: int | None = None
    members: list[MemberSummary] | None = None
    raw: JSONDict | None = None


class ResponseCounts(BaseModel):
    model_config = _strict()

    accepted: int = 0
    declined: int = 0
    unanswered: int = 0
    waiting_list: int = 0
    unknown: int = 0


class EventSummary(BaseModel):
    model_config = _strict()

    event_id: str
    heading: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    start: str | None = None
    end: str | None = None
    location_name: str | None = None
    cancelled: bool = False
    response_counts: ResponseCounts | None = None
    raw: JSONDict | None = None


class AttendanceSummary(BaseModel):
    model_config = _strict()

    event_id: str
    counts: ResponseCounts
    accepted_member_ids: list[str] = Field(default_factory=list)
    declined_member_ids: list[str] = Field(default_factory=list)
    unanswered_member_ids: list[str] = Field(default_factory=list)


class EventDetail(BaseModel):
    model_config = _strict()

    event_id: str
    heading: str | None = None
    description: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    start: str | None = None
    end: str | None = None
    location_name: str | None = None
    location_address: str | None = None
    cancelled: bool = False
    attendance: AttendanceSummary | None = None
    raw: JSONDict | None = None


class ChatSummary(BaseModel):
    model_config = _strict()

    chat_id: str | None = None
    title: str | None = None
    last_message_at: str | None = None
    last_sender: str | None = None
    text_preview: str | None = None
    unread: int | None = None
    raw: JSONDict | None = None


class PostSummary(BaseModel):
    model_config = _strict()

    post_id: str | None = None
    group_id: str | None = None
    author: str | None = None
    created_at: str | None = None
    title: str | None = None
    text_preview: str | None = None
    comment_count: int | None = None
    raw: JSONDict | None = None


class TransactionSummary(BaseModel):
    model_config = _strict()

    transaction_id: str | None = None
    created_at: str | None = None
    description: str | None = None
    amount: float | None = None
    currency: str | None = None
    status: str | None = None
    payer_name: str | None = None
    raw: JSONDict | None = None


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def _full_name(d: JSONDict) -> str | None:
    first = d.get("firstName") or d.get("first_name")
    last = d.get("lastName") or d.get("last_name")
    if first and last:
        return f"{first} {last}".strip()
    return first or last or d.get("name")


def map_profile(profile: JSONDict, *, include_raw: bool = False) -> ProfileSummary:
    profile = profile or {}
    contact = profile.get("contactInfo") or profile.get("contact") or {}
    return ProfileSummary(
        profile_id=profile.get("id") or profile.get("profileId"),
        full_name=_full_name(profile),
        email=profile.get("email") or contact.get("email"),
        phone=profile.get("phoneNumber") or contact.get("phone"),
        locale=profile.get("language") or profile.get("locale"),
        raw=profile if include_raw else None,
    )


def map_member(member: JSONDict, *, is_guardian: bool = False, include_raw: bool = False) -> MemberSummary:
    member = member or {}
    profile = member.get("profile") or {}
    return MemberSummary(
        member_id=member.get("id"),
        profile_id=profile.get("id") if isinstance(profile, dict) else None,
        full_name=_full_name(member),
        email=member.get("email"),
        role=member.get("role") or member.get("roleTitle"),
        is_guardian=is_guardian,
        raw=member if include_raw else None,
    )


def map_group(
    group: JSONDict,
    *,
    include_members: bool = False,
    include_raw: bool = False,
) -> GroupSummary:
    group = group or {}
    members_raw = group.get("members") or []
    subgroups = group.get("subGroups") or group.get("subgroups") or []
    members: list[MemberSummary] | None = None
    if include_members:
        members = [map_member(m, include_raw=include_raw) for m in members_raw]
    return GroupSummary(
        group_id=group.get("id", ""),
        name=group.get("name"),
        description=group.get("description"),
        member_count=len(members_raw) if isinstance(members_raw, list) else None,
        subgroup_count=len(subgroups) if isinstance(subgroups, list) else None,
        members=members,
        raw=group if include_raw else None,
    )


def _response_counts(responses: JSONDict | None) -> ResponseCounts:
    if not responses:
        return ResponseCounts()
    accepted = responses.get("acceptedIds") or []
    declined = responses.get("declinedIds") or []
    unanswered = responses.get("unansweredIds") or []
    waiting = responses.get("waitinglistIds") or responses.get("waitingListIds") or []
    unknown = responses.get("unconfirmedIds") or []
    return ResponseCounts(
        accepted=len(accepted),
        declined=len(declined),
        unanswered=len(unanswered),
        waiting_list=len(waiting),
        unknown=len(unknown),
    )


def map_event_summary(event: JSONDict, *, include_raw: bool = False) -> EventSummary:
    event = event or {}
    location = event.get("location") or {}
    owners = event.get("owners") or {}
    group = event.get("group") or {}
    if not group and isinstance(owners, dict):
        group = owners.get("group") or {}
    responses = event.get("responses") or {}
    return EventSummary(
        event_id=event.get("id", ""),
        heading=event.get("heading") or event.get("name"),
        group_id=group.get("id") if isinstance(group, dict) else None,
        group_name=group.get("name") if isinstance(group, dict) else None,
        start=normalize_iso(event.get("startTimestamp")),
        end=normalize_iso(event.get("endTimestamp")),
        location_name=location.get("feature") or location.get("name") if isinstance(location, dict) else None,
        cancelled=bool(event.get("cancelled")),
        response_counts=_response_counts(responses) if responses else None,
        raw=event if include_raw else None,
    )


def map_event_detail(event: JSONDict, *, include_responses: bool = True, include_raw: bool = False) -> EventDetail:
    event = event or {}
    location = event.get("location") or {}
    group = event.get("group") or {}
    if not group:
        owners = event.get("owners") or {}
        if isinstance(owners, dict):
            group = owners.get("group") or {}
    responses = event.get("responses") or {}
    attendance: AttendanceSummary | None = None
    if include_responses and responses:
        attendance = AttendanceSummary(
            event_id=event.get("id", ""),
            counts=_response_counts(responses),
            accepted_member_ids=list(responses.get("acceptedIds") or []),
            declined_member_ids=list(responses.get("declinedIds") or []),
            unanswered_member_ids=list(responses.get("unansweredIds") or []),
        )
    return EventDetail(
        event_id=event.get("id", ""),
        heading=event.get("heading") or event.get("name"),
        description=_truncate(event.get("description"), 1000),
        group_id=group.get("id") if isinstance(group, dict) else None,
        group_name=group.get("name") if isinstance(group, dict) else None,
        start=normalize_iso(event.get("startTimestamp")),
        end=normalize_iso(event.get("endTimestamp")),
        location_name=location.get("feature") or location.get("name") if isinstance(location, dict) else None,
        location_address=location.get("address") if isinstance(location, dict) else None,
        cancelled=bool(event.get("cancelled")),
        attendance=attendance,
        raw=event if include_raw else None,
    )


def map_chat(chat: JSONDict, *, include_raw: bool = False) -> ChatSummary:
    chat = chat or {}
    last = chat.get("lastMessage") or {}
    sender = last.get("sender") or {}
    sender_name = _full_name(sender) if isinstance(sender, dict) else None
    return ChatSummary(
        chat_id=chat.get("id") or chat.get("chatId"),
        title=chat.get("name") or chat.get("title"),
        last_message_at=normalize_iso(last.get("timestamp") or chat.get("lastMessageAt")),
        last_sender=sender_name,
        text_preview=_truncate(last.get("text") or last.get("body")),
        unread=chat.get("unread") or chat.get("unreadCount"),
        raw=chat if include_raw else None,
    )


def map_post(post: JSONDict, *, include_raw: bool = False) -> PostSummary:
    post = post or {}
    author = post.get("author") or post.get("createdBy") or {}
    comments = post.get("comments") or []
    return PostSummary(
        post_id=post.get("id"),
        group_id=(post.get("group") or {}).get("id") if isinstance(post.get("group"), dict) else post.get("groupId"),
        author=_full_name(author) if isinstance(author, dict) else None,
        created_at=normalize_iso(post.get("createdTime") or post.get("timestamp")),
        title=post.get("title"),
        text_preview=_truncate(post.get("body") or post.get("text"), 280),
        comment_count=len(comments) if isinstance(comments, list) else None,
        raw=post if include_raw else None,
    )


def map_transaction(tx: JSONDict, *, include_raw: bool = False) -> TransactionSummary:
    tx = tx or {}
    payer = tx.get("payer") or tx.get("from") or {}
    return TransactionSummary(
        transaction_id=tx.get("id") or tx.get("transactionId"),
        created_at=normalize_iso(tx.get("createdAt") or tx.get("timestamp") or tx.get("date")),
        description=tx.get("description") or tx.get("title"),
        amount=tx.get("amount") if isinstance(tx.get("amount"), (int, float)) else None,
        currency=tx.get("currency"),
        status=tx.get("status"),
        payer_name=_full_name(payer) if isinstance(payer, dict) else None,
        raw=tx if include_raw else None,
    )
