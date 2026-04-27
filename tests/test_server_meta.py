"""Tool/resource/prompt registration metadata."""

from __future__ import annotations

import pytest

from spond_mcp.server import build_server

EXPECTED_TOOLS = {
    "spond_get_profile",
    "spond_list_groups",
    "spond_get_group",
    "spond_find_person",
    "spond_list_events",
    "spond_get_event",
    "spond_summarize_schedule",
    "spond_get_event_attendance_report",
    "spond_change_event_response",
    "spond_list_messages",
    "spond_send_message",
    "spond_list_posts",
    "spond_list_club_transactions",
}

EXPECTED_RESOURCES = {
    "spond://profile",
    "spond://groups",
    "spond://events/upcoming",
    "spond://schedule/today",
    "spond://schedule/week",
}

EXPECTED_PROMPTS = {
    "summarize_weekly_spond_schedule",
    "prepare_attendance_decision",
    "draft_spond_message",
}


@pytest.mark.asyncio
async def test_all_tools_resources_prompts_registered(manager):
    mcp, _ = build_server(manager.settings, manager=manager)
    tools = {t.name for t in await mcp.list_tools()}
    resources = {str(r.uri) for r in await mcp.list_resources()}
    prompts = {p.name for p in await mcp.list_prompts()}
    assert EXPECTED_TOOLS.issubset(tools)
    assert EXPECTED_RESOURCES.issubset(resources)
    assert EXPECTED_PROMPTS.issubset(prompts)


@pytest.mark.asyncio
async def test_tool_descriptions_have_no_imperatives_to_agent(manager):
    """Tool descriptions must be factual, not instructions to the model."""

    mcp, _ = build_server(manager.settings, manager=manager)
    tools = await mcp.list_tools()
    forbidden_phrases = ["you must", "ignore previous", "as an ai"]
    for t in tools:
        desc = (t.description or "").lower()
        for phrase in forbidden_phrases:
            assert phrase not in desc, f"{t.name} description contains '{phrase}'"
