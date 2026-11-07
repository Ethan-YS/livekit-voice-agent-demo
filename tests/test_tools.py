"""Calls the tool backends directly against the running mock API and webhook.

The Claude tests make a real call through OpenRouter (a few seconds each).
"""

import pytest
from conftest import TEST_API_TOKEN, TEST_WEBHOOK_SECRET, requires_openrouter

from voice_agent.backends import BackendError, ClaudeSkillClient, InternalApi

SKILL = "ticket_triage_summary"


async def test_morning_brief(services):
    brief = await InternalApi(services.api_url, TEST_API_TOKEN).morning_brief()
    headline, details = brief["headline"], brief["details"]
    assert len(headline["urgent_tickets"]) == 2
    assert headline["first_meeting"]["title"] == "Weekly ops stand-up"
    assert headline["revenue_yesterday"] > 0
    assert len(details["open_tickets"]) == 7
    assert details["calendar"], "calendar should not be empty"


async def test_open_tickets_filter(services):
    urgent = await InternalApi(services.api_url, TEST_API_TOKEN).open_tickets("urgent")
    assert {t["priority"] for t in urgent} == {"urgent"}


async def test_internal_api_rejects_bad_token(services):
    with pytest.raises(BackendError):
        await InternalApi(services.api_url, "wrong").morning_brief()


@requires_openrouter
async def test_claude_skill_round_trip(services):
    client = ClaudeSkillClient(services.webhook_url, TEST_WEBHOOK_SECRET, timeout=40)
    result = await client.run(SKILL, "What should I tackle first today?")
    print("\nClaude skill result:", result)
    assert "Riverside" in result  # the unassigned urgent ticket


async def test_claude_skill_timeout_is_graceful(services):
    client = ClaudeSkillClient(services.webhook_url, TEST_WEBHOOK_SECRET, timeout=0.3)
    with pytest.raises(BackendError, match="did not answer in time"):
        await client.run(SKILL, "What should I tackle first today?")


async def test_claude_skill_rejects_bad_secret(services):
    with pytest.raises(BackendError):
        await ClaudeSkillClient(services.webhook_url, "wrong").run(SKILL, "hi")
