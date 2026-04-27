"""FastMCP server exposing curated Spond tools, resources, and prompts."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base as prompt_base
from pydantic import Field

from . import __version__
from .client import SpondClientManager
from .config import Settings, load_settings
from .errors import (
    SpondMcpError,
    SpondNotFoundError,
    SpondPolicyError,
    SpondUnsupportedError,
    SpondValidationError,
)
from .schemas import (
    AttendanceSummary,
    ChatSummary,
    EventDetail,
    EventSummary,
    GroupSummary,
    JSONDict,
    MemberSummary,
    PostSummary,
    ProfileSummary,
    ResponseLiteral,
    ToolError,
    TransactionSummary,
    map_chat,
    map_event_detail,
    map_event_summary,
    map_group,
    map_member,
    map_post,
    map_profile,
    map_transaction,
    parse_iso_datetime,
)

logger = logging.getLogger("spond_mcp.server")

JSONResult = dict[str, Any]


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def _to_dict(obj: Any) -> JSONResult:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if isinstance(obj, list):
        return {"items": [_to_dict(x) for x in obj]}
    return {"value": obj}


def _error(exc: SpondMcpError) -> JSONResult:
    return ToolError(code=exc.code, message=exc.message, details=exc.details).model_dump()


def _check_raw_allowed(settings: Settings, requested: bool) -> None:
    """Reject ``include_raw=true`` unless the operator has opted in.

    Raw upstream payloads contain PII (member contact details), full message
    bodies, and raw financial records. They must never be returned by default.
    """

    if requested and not settings.raw_payloads_allowed():
        raise SpondPolicyError(
            "Raw upstream payloads are disabled by policy.",
            details={
                "hint": (
                    "Set SPOND_MCP_ALLOW_RAW_PAYLOADS=true to permit include_raw "
                    "responses. Compact summaries are returned without it."
                ),
            },
        )


# Allow conservative filename-safe characters only. Spond event ids look like
# UUID-style hex+dashes, so this is forgiving enough; anything outside is
# replaced.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_FILENAME_STEM = 64


def _safe_xlsx_filename(event_id: str) -> str:
    """Build a deterministic XLSX filename that is safe to write to disk.

    - strips path separators and any character outside ``[A-Za-z0-9_-]``
    - strips leading dots so the filename cannot become hidden / traversal
    - bounds the stem to 64 characters
    - always returns a name ending in ``.xlsx``
    - falls back to a stable name if the input has no safe characters
    """

    cleaned = _SAFE_FILENAME_RE.sub("_", event_id or "").lstrip(".")
    cleaned = cleaned[:_MAX_FILENAME_STEM] or "event"
    return f"spond-attendance-{cleaned}.xlsx"


def _check_contact_allowed(settings: Settings, requested: bool) -> None:
    """Reject ``include_contact=true`` unless the operator has opted in.

    Member email and phone numbers are PII. They are stripped from default
    summaries so an autonomous agent does not accidentally exfiltrate contact
    details into a transcript or downstream tool call.
    """

    if requested and not settings.contact_details_allowed():
        raise SpondPolicyError(
            "Contact details are disabled by policy.",
            details={
                "hint": (
                    "Set SPOND_MCP_ALLOW_CONTACT_DETAILS=true to permit "
                    "include_contact responses. Names and IDs are returned "
                    "without it."
                ),
            },
        )


def _wrap_tool(fn):
    """Decorator that translates SpondMcpError to a uniform JSON error."""

    async def _inner(*args: Any, **kwargs: Any) -> JSONResult:
        try:
            return await fn(*args, **kwargs)
        except SpondMcpError as exc:
            return _error(exc)
        except ValueError as exc:
            return _error(SpondValidationError(str(exc)))

    _inner.__name__ = fn.__name__
    _inner.__doc__ = fn.__doc__
    _inner.__wrapped__ = fn  # type: ignore[attr-defined]
    return _inner


def build_server(
    settings: Settings | None = None,
    *,
    manager: SpondClientManager | None = None,
) -> tuple[FastMCP, SpondClientManager]:
    """Create the MCP server and its client manager.

    Returning the manager lets callers (and tests) close it cleanly.
    """

    settings = settings or load_settings()
    manager = manager or SpondClientManager(settings)

    mcp = FastMCP(
        name="spond-mcp",
        instructions=(
            "Tools wrap the unofficial Spond API. Side-effecting tools require "
            "explicit policy flags and confirm=true."
        ),
    )

    # ----------------------------------------------------------------- tools

    @mcp.tool(
        name="spond_get_profile",
        description=(
            "Get the authenticated Spond account profile. Email and phone are "
            "omitted unless include_contact=true and "
            "SPOND_MCP_ALLOW_CONTACT_DETAILS=true."
        ),
    )
    @_wrap_tool
    async def spond_get_profile(
        include_contact: Annotated[
            bool, Field(description="Include email and phone (PII) in the response")
        ] = False,
        include_raw: Annotated[bool, Field(description="Include the raw upstream payload")] = False,
        refresh: Annotated[bool, Field(description="Bypass the in-memory cache")] = False,
    ) -> JSONResult:
        _check_raw_allowed(settings, include_raw)
        _check_contact_allowed(settings, include_contact)

        async def _fetch() -> JSONDict:
            client = await manager.get_spond()
            return await manager.call("get_profile", client.get_profile)

        data = await manager.cache.get_or_set("profile", _fetch, refresh=refresh)
        return map_profile(
            data, include_raw=include_raw, include_contact=include_contact
        ).model_dump(exclude_none=True)

    @mcp.tool(
        name="spond_list_groups",
        description=(
            "List groups the authenticated user can access. Member email and "
            "phone are omitted unless include_contact=true and "
            "SPOND_MCP_ALLOW_CONTACT_DETAILS=true."
        ),
    )
    @_wrap_tool
    async def spond_list_groups(
        include_members: bool = False,
        include_contact: bool = False,
        include_raw: bool = False,
        refresh: bool = False,
    ) -> JSONResult:
        _check_raw_allowed(settings, include_raw)
        _check_contact_allowed(settings, include_contact)

        async def _fetch() -> list[JSONDict]:
            client = await manager.get_spond()
            return await manager.call("get_groups", client.get_groups) or []

        groups = await manager.cache.get_or_set("groups", _fetch, refresh=refresh)
        return {
            "groups": [
                map_group(
                    g,
                    include_members=include_members,
                    include_raw=include_raw,
                    include_contact=include_contact,
                ).model_dump(exclude_none=True)
                for g in groups
            ],
            "count": len(groups),
        }

    @mcp.tool(
        name="spond_get_group",
        description="Get details for one group, optionally including members.",
    )
    @_wrap_tool
    async def spond_get_group(
        group_id: str,
        include_members: bool = True,
        include_contact: bool = False,
        include_raw: bool = False,
        refresh: bool = False,
    ) -> JSONResult:
        if not group_id:
            raise SpondValidationError("group_id is required")
        _check_raw_allowed(settings, include_raw)
        _check_contact_allowed(settings, include_contact)

        async def _fetch() -> JSONDict:
            client = await manager.get_spond()
            return await manager.call("get_group", lambda: client.get_group(group_id))

        cache_key = f"group:{group_id}"
        group = await manager.cache.get_or_set(cache_key, _fetch, refresh=refresh)
        return map_group(
            group,
            include_members=include_members,
            include_raw=include_raw,
            include_contact=include_contact,
        ).model_dump(exclude_none=True)

    @mcp.tool(
        name="spond_find_person",
        description="Find a member or guardian by id, profile id, email, or full name.",
    )
    @_wrap_tool
    async def spond_find_person(
        query: str,
        group_id: str | None = None,
        include_contact: bool = False,
        include_raw: bool = False,
    ) -> JSONResult:
        if not query or not query.strip():
            raise SpondValidationError("query must not be empty")
        _check_raw_allowed(settings, include_raw)
        _check_contact_allowed(settings, include_contact)

        client = await manager.get_spond()
        if group_id:
            group = await manager.call("get_group", lambda: client.get_group(group_id))
            for member in group.get("members") or []:
                if _match_person(member, query):
                    return _person_result(
                        member, include_raw=include_raw, include_contact=include_contact
                    )
                for guardian in member.get("guardians") or []:
                    if _match_person(guardian, query):
                        return _person_result(
                            guardian,
                            include_raw=include_raw,
                            include_contact=include_contact,
                            is_guardian=True,
                        )
            raise SpondNotFoundError(f"No person matched '{query}' in group '{group_id}'")

        try:
            person = await manager.call("get_person", lambda: client.get_person(query))
        except SpondNotFoundError:
            raise SpondNotFoundError(f"No person matched '{query}'") from None
        return _person_result(
            person, include_raw=include_raw, include_contact=include_contact
        )

    @mcp.tool(
        name="spond_list_events",
        description=(
            "List events visible to the user with optional date, group, and subgroup "
            "filters. Datetimes must be ISO-8601."
        ),
    )
    @_wrap_tool
    async def spond_list_events(
        group_id: str | None = None,
        subgroup_id: str | None = None,
        from_datetime: str | None = None,
        to_datetime: str | None = None,
        include_scheduled: bool = False,
        include_hidden: bool = False,
        max_events: int = 100,
        include_raw: bool = False,
        refresh: bool = False,
    ) -> JSONResult:
        _check_raw_allowed(settings, include_raw)
        try:
            min_start = parse_iso_datetime(from_datetime)
            max_start = parse_iso_datetime(to_datetime)
        except ValueError as exc:
            raise SpondValidationError(str(exc)) from exc

        cap = settings.spond_mcp_max_events
        capped_max = max(1, min(int(max_events), cap))

        async def _fetch() -> list[JSONDict]:
            client = await manager.get_spond()
            return (
                await manager.call(
                    "get_events",
                    lambda: client.get_events(
                        group_id=group_id,
                        subgroup_id=subgroup_id,
                        include_scheduled=include_scheduled,
                        include_hidden=include_hidden,
                        max_start=max_start,
                        min_start=min_start,
                        max_events=capped_max,
                    ),
                )
                or []
            )

        cache_key = json.dumps(
            {
                "g": group_id,
                "sg": subgroup_id,
                "f": from_datetime,
                "t": to_datetime,
                "sched": include_scheduled,
                "hid": include_hidden,
                "n": capped_max,
            },
            sort_keys=True,
        )
        events = await manager.cache.get_or_set(f"events:{cache_key}", _fetch, refresh=refresh)
        return {
            "events": [
                map_event_summary(e, include_raw=include_raw).model_dump(exclude_none=True)
                for e in events
            ],
            "count": len(events),
            "max_events_applied": capped_max,
        }

    @mcp.tool(
        name="spond_get_event",
        description="Get one event by id, including attendance/response summary.",
    )
    @_wrap_tool
    async def spond_get_event(
        event_id: str,
        include_responses: bool = True,
        include_raw: bool = False,
        refresh: bool = False,
    ) -> JSONResult:
        if not event_id:
            raise SpondValidationError("event_id is required")
        _check_raw_allowed(settings, include_raw)

        async def _fetch() -> JSONDict:
            client = await manager.get_spond()
            return await manager.call("get_event", lambda: client.get_event(event_id))

        cache_key = f"event:{event_id}"
        event = await manager.cache.get_or_set(cache_key, _fetch, refresh=refresh)
        return map_event_detail(
            event, include_responses=include_responses, include_raw=include_raw
        ).model_dump(exclude_none=True)

    @mcp.tool(
        name="spond_summarize_schedule",
        description=(
            "Summarize events between two ISO-8601 datetimes, grouped by day, in a "
            "compact form suitable for agent reasoning."
        ),
    )
    @_wrap_tool
    async def spond_summarize_schedule(
        from_datetime: str,
        to_datetime: str,
        group_id: str | None = None,
        max_events: int = 100,
    ) -> JSONResult:
        result = await spond_list_events.__wrapped__(  # type: ignore[attr-defined]
            group_id=group_id,
            from_datetime=from_datetime,
            to_datetime=to_datetime,
            max_events=max_events,
        )
        events = result.get("events", []) if isinstance(result, dict) else []
        by_day: dict[str, list[dict[str, Any]]] = {}
        for ev in events:
            start = ev.get("start") or ""
            day = start[:10] if start else "unknown"
            by_day.setdefault(day, []).append(
                {
                    "event_id": ev.get("event_id"),
                    "heading": ev.get("heading"),
                    "start": ev.get("start"),
                    "end": ev.get("end"),
                    "location": ev.get("location_name"),
                    "group": ev.get("group_name"),
                }
            )
        days = [
            {"date": day, "events": sorted(items, key=lambda e: e.get("start") or "")}
            for day, items in sorted(by_day.items())
        ]
        return {
            "from": from_datetime,
            "to": to_datetime,
            "total_events": len(events),
            "days": days,
        }

    @mcp.tool(
        name="spond_get_event_attendance_report",
        description=(
            "Export an event attendance report. Returns XLSX metadata only by "
            "default. mode='tempfile' writes the XLSX to a sanitised temp file "
            "and returns its path; that branch requires "
            "SPOND_MCP_ALLOW_FILE_EXPORTS=true."
        ),
    )
    @_wrap_tool
    async def spond_get_event_attendance_report(
        event_id: str,
        mode: Literal["metadata", "tempfile"] = "metadata",
    ) -> JSONResult:
        if not event_id:
            raise SpondValidationError("event_id is required")

        # Gate the local-disk side effect *before* hitting the network so a
        # disabled deployment never even fetches the bytes it cannot return.
        if mode == "tempfile" and not settings.file_exports_allowed():
            raise SpondPolicyError(
                "Writing attendance reports to disk is disabled by policy.",
                details={
                    "hint": (
                        "Set SPOND_MCP_ALLOW_FILE_EXPORTS=true to enable "
                        "tempfile mode. Use mode='metadata' for a side-effect"
                        "-free response."
                    ),
                },
            )

        filename = _safe_xlsx_filename(event_id)
        client = await manager.get_spond()
        data: bytes = await manager.call(
            "get_event_attendance_xlsx",
            lambda: client.get_event_attendance_xlsx(event_id),
        )
        info: JSONResult = {
            "event_id": event_id,
            "filename": filename,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "byte_size": len(data),
        }
        if mode == "tempfile":
            tmp_dir = tempfile.mkdtemp(prefix="spond_mcp_")
            # `filename` has been sanitised; join is safe relative to tmp_dir.
            path = os.path.join(tmp_dir, filename)
            with open(path, "wb") as fh:
                fh.write(data)
            info["path"] = path
        return info

    @mcp.tool(
        name="spond_change_event_response",
        description=(
            "Change a user's response for an event. Accepts response='accepted' "
            "or response='declined'. Requires SPOND_MCP_ALLOW_ATTENDANCE_CHANGES"
            "=true and confirm=true."
        ),
    )
    @_wrap_tool
    async def spond_change_event_response(
        event_id: str,
        user_id: str,
        response: ResponseLiteral,
        confirm: bool = False,
    ) -> JSONResult:
        if not settings.attendance_changes_allowed():
            raise SpondPolicyError(
                "Attendance changes are disabled by policy.",
                details={
                    "read_only": settings.spond_mcp_read_only,
                    "allow_attendance_changes": settings.spond_mcp_allow_attendance_changes,
                },
            )
        if not confirm:
            raise SpondPolicyError("Refusing to change attendance without confirm=true.")
        if not event_id or not user_id:
            raise SpondValidationError("event_id and user_id are required")

        payload = _response_payload(response)
        client = await manager.get_spond()
        result = await manager.call(
            "change_response",
            lambda: client.change_response(event_id, user_id, payload),
        )
        manager.cache.invalidate(f"event:{event_id}")
        return {
            "event_id": event_id,
            "user_id": user_id,
            "response": response,
            "responses": result if isinstance(result, dict) else {"raw": result},
        }

    @mcp.tool(
        name="spond_list_messages",
        description=(
            "List recent chats. Message bodies are truncated to a short preview. "
            "Returning the full upstream payload requires "
            "SPOND_MCP_ALLOW_RAW_PAYLOADS=true."
        ),
    )
    @_wrap_tool
    async def spond_list_messages(
        max_chats: int = 50,
        include_raw: bool = False,
    ) -> JSONResult:
        if max_chats <= 0:
            raise SpondValidationError("max_chats must be positive")
        _check_raw_allowed(settings, include_raw)
        client = await manager.get_spond()
        chats = (
            await manager.call("get_messages", lambda: client.get_messages(max_chats=max_chats))
            or []
        )
        return {
            "chats": [map_chat(c, include_raw=include_raw).model_dump(exclude_none=True) for c in chats],
            "count": len(chats),
        }

    @mcp.tool(
        name="spond_send_message",
        description=(
            "Send a Spond chat message. Routing is exclusive: either continue an "
            "existing chat by chat_id, or start a new chat by providing both user "
            "and group_id. Requires SPOND_MCP_ALLOW_MESSAGES=true and confirm=true."
        ),
    )
    @_wrap_tool
    async def spond_send_message(
        text: str,
        chat_id: str | None = None,
        user: str | None = None,
        group_id: str | None = None,
        confirm: bool = False,
    ) -> JSONResult:
        if not settings.messages_allowed():
            raise SpondPolicyError(
                "Sending messages is disabled by policy.",
                details={
                    "read_only": settings.spond_mcp_read_only,
                    "allow_messages": settings.spond_mcp_allow_messages,
                },
            )
        if not confirm:
            raise SpondPolicyError("Refusing to send a message without confirm=true.")
        if not text or not text.strip():
            raise SpondValidationError("text must not be empty")
        if len(text) > 4000:
            raise SpondValidationError("text exceeds the 4000-character limit")

        # Exactly one routing mode: existing chat OR new chat.
        if chat_id is not None and (user is not None or group_id is not None):
            raise SpondValidationError(
                "Ambiguous routing: provide either chat_id (existing chat) or "
                "both user and group_id (new chat), but not both.",
            )
        if chat_id is None and (user is None or group_id is None):
            raise SpondValidationError(
                "Provide either chat_id or both user and group_id.",
            )

        client = await manager.get_spond()
        result = await manager.call(
            "send_message",
            lambda: client.send_message(
                text=text, user=user, group_uid=group_id, chat_id=chat_id
            ),
        )
        # The upstream `send_message` has a known bug: in the chat_id path it
        # returns the un-awaited coroutine produced by `_continue_chat`. Drain
        # it through the manager so any failure inside the continuation is
        # sanitised by the same exception path as `call()` (no leaking of
        # request bodies, tokens, or outgoing message text).
        result = await manager.resolve_awaitable_result("send_message", result)

        # Treat any dict with an "error" key as a failure but never echo the
        # body back to the caller.
        sent_id = None
        ok = True
        if isinstance(result, dict):
            if result.get("error"):
                raise SpondValidationError(str(result.get("error")))
            sent_id = result.get("id") or result.get("messageId")
        elif result is False:
            ok = False
        return {
            "sent": ok,
            "chat_id": chat_id,
            "message_id": sent_id,
            "text_length": len(text),
        }

    @mcp.tool(
        name="spond_list_posts",
        description="List group wall posts; returns text previews by default.",
    )
    @_wrap_tool
    async def spond_list_posts(
        max_posts: int = 50,
        group_id: str | None = None,
        include_raw: bool = False,
        refresh: bool = False,
    ) -> JSONResult:
        _check_raw_allowed(settings, include_raw)
        client = await manager.get_spond()
        if not hasattr(client, "get_posts"):
            raise SpondUnsupportedError(
                "The installed spond library does not expose get_posts(). "
                "Upgrade `spond` to a version that supports posts."
            )

        async def _fetch() -> list[JSONDict]:
            return (
                await manager.call(
                    "get_posts",
                    lambda: client.get_posts(
                        group_id=group_id, max_posts=max_posts, include_comments=False
                    ),
                )
                or []
            )

        cache_key = f"posts:{group_id}:{max_posts}"
        posts = await manager.cache.get_or_set(cache_key, _fetch, refresh=refresh)
        return {
            "posts": [map_post(p, include_raw=include_raw).model_dump(exclude_none=True) for p in posts],
            "count": len(posts),
        }

    @mcp.tool(
        name="spond_list_club_transactions",
        description="List Spond Club finance transactions for the configured club.",
    )
    @_wrap_tool
    async def spond_list_club_transactions(
        club_id: str | None = None,
        max_items: int = 100,
        include_raw: bool = False,
    ) -> JSONResult:
        effective_club = club_id or settings.spond_club_id
        if not effective_club:
            raise SpondValidationError(
                "No club id provided and SPOND_CLUB_ID is not configured."
            )
        if max_items <= 0:
            raise SpondValidationError("max_items must be positive")
        _check_raw_allowed(settings, include_raw)

        club = await manager.get_club()
        txs = (
            await manager.call(
                "get_transactions",
                lambda: club.get_transactions(club_id=effective_club, max_items=max_items),
            )
            or []
        )
        return {
            "club_id": effective_club,
            "transactions": [
                map_transaction(t, include_raw=include_raw).model_dump(exclude_none=True)
                for t in txs
            ],
            "count": len(txs),
        }

    # ------------------------------------------------------------- resources

    @mcp.resource("spond://profile", description="Cached profile of the authenticated Spond account.")
    async def res_profile() -> str:
        try:
            return _json(await spond_get_profile.__wrapped__())  # type: ignore[attr-defined]
        except SpondMcpError as exc:
            return _json(_error(exc))

    @mcp.resource("spond://groups", description="Cached list of accessible Spond groups.")
    async def res_groups() -> str:
        try:
            return _json(await spond_list_groups.__wrapped__())  # type: ignore[attr-defined]
        except SpondMcpError as exc:
            return _json(_error(exc))

    @mcp.resource(
        "spond://events/upcoming",
        description="Up to 50 upcoming events from now into the future.",
    )
    async def res_upcoming() -> str:
        now = datetime.now(UTC)
        try:
            return _json(
                await spond_list_events.__wrapped__(  # type: ignore[attr-defined]
                    from_datetime=now.isoformat(),
                    max_events=50,
                )
            )
        except SpondMcpError as exc:
            return _json(_error(exc))

    @mcp.resource("spond://schedule/today", description="Today's schedule in UTC.")
    async def res_today() -> str:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        try:
            return _json(
                await spond_summarize_schedule.__wrapped__(  # type: ignore[attr-defined]
                    from_datetime=start.isoformat(),
                    to_datetime=end.isoformat(),
                )
            )
        except SpondMcpError as exc:
            return _json(_error(exc))

    @mcp.resource("spond://schedule/week", description="Schedule for the next 7 days in UTC.")
    async def res_week() -> str:
        start = datetime.now(UTC)
        end = start + timedelta(days=7)
        try:
            return _json(
                await spond_summarize_schedule.__wrapped__(  # type: ignore[attr-defined]
                    from_datetime=start.isoformat(),
                    to_datetime=end.isoformat(),
                )
            )
        except SpondMcpError as exc:
            return _json(_error(exc))

    # --------------------------------------------------------------- prompts

    @mcp.prompt(
        name="summarize_weekly_spond_schedule",
        description="Build a concise weekly schedule summary from Spond events.",
    )
    def prompt_weekly_schedule(group_id: str | None = None) -> list[prompt_base.Message]:
        scope = f" for group {group_id}" if group_id else ""
        return [
            prompt_base.UserMessage(
                "Use spond_summarize_schedule with from_datetime=start of this week (UTC) "
                f"and to_datetime=start of next week to retrieve events{scope}. "
                "Then produce a concise day-by-day bullet list including time, heading, "
                "and location. Mention cancelled events explicitly. Do not invent events."
            )
        ]

    @mcp.prompt(
        name="prepare_attendance_decision",
        description="Inspect an event before deciding to accept or decline.",
    )
    def prompt_attendance(event_id: str) -> list[prompt_base.Message]:
        return [
            prompt_base.UserMessage(
                f"Call spond_get_event with event_id={event_id!r} to load timing, "
                "location, and current response counts. Summarize the event and the "
                "open questions the user should answer (conflicts, travel time, "
                "guests). Do not call spond_change_event_response. Ask the user "
                "whether to accept, decline, or stay unanswered, and only invoke the "
                "change tool after the user explicitly confirms."
            )
        ]

    @mcp.prompt(
        name="draft_spond_message",
        description="Help draft a Spond message; never send without explicit confirmation.",
    )
    def prompt_draft_message(
        recipient: str,
        topic: str,
        tone: str = "friendly",
    ) -> list[prompt_base.Message]:
        return [
            prompt_base.UserMessage(
                f"Draft a {tone} Spond message to {recipient} about: {topic}. "
                "Show the draft to the user and wait for explicit confirmation. "
                "Only after the user confirms, call spond_send_message with "
                "confirm=true. If policy disables messaging, tell the user instead "
                "of attempting to send."
            )
        ]

    return mcp, manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _match_person(person: JSONDict, query: str) -> bool:
    query = query.strip()
    profile = person.get("profile") or {}
    return (
        person.get("id") == query
        or person.get("email") == query
        or (profile.get("id") if isinstance(profile, dict) else None) == query
        or _full_name(person) == query
    )


def _full_name(person: JSONDict) -> str | None:
    first = person.get("firstName") or person.get("first_name")
    last = person.get("lastName") or person.get("last_name")
    if first and last:
        return f"{first} {last}".strip()
    return first or last


def _person_result(
    person: JSONDict,
    *,
    include_raw: bool = False,
    include_contact: bool = False,
    is_guardian: bool = False,
) -> JSONResult:
    return map_member(
        person,
        is_guardian=is_guardian,
        include_raw=include_raw,
        include_contact=include_contact,
    ).model_dump(exclude_none=True)


def _response_payload(response: ResponseLiteral) -> JSONDict:
    """Translate the curated enum into the documented upstream payload."""

    if response == "accepted":
        return {"accepted": "true"}
    if response == "declined":
        return {"accepted": "false"}
    raise SpondValidationError(f"Unsupported response value: {response!r}")


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover - thin stdio wrapper
    """Run the server over stdio."""

    logging.basicConfig(
        level=os.environ.get("SPOND_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    settings = load_settings()
    mcp, manager = build_server(settings=settings)
    logger.info(
        "spond-mcp %s starting (read_only=%s, messages=%s, attendance=%s)",
        __version__,
        settings.spond_mcp_read_only,
        settings.spond_mcp_allow_messages,
        settings.spond_mcp_allow_attendance_changes,
    )
    try:
        mcp.run()
    finally:
        import asyncio

        try:
            asyncio.run(manager.aclose())
        except Exception:
            logger.debug("error during shutdown", exc_info=True)


if __name__ == "__main__":  # pragma: no cover
    main()


# Public re-exports so tests can introspect easily.
__all__ = [
    "AttendanceSummary",
    "ChatSummary",
    "EventDetail",
    "EventSummary",
    "GroupSummary",
    "MemberSummary",
    "PostSummary",
    "ProfileSummary",
    "ResponseLiteral",
    "TransactionSummary",
    "build_server",
    "main",
]
