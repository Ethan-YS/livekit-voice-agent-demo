"""Behaviour tests: does the LLM pick the right tool? Text in, text out, no audio.

Uses the LiveKit Agents test harness (AgentSession.run) with the real LLM, so it
makes real API calls (Claude by default).
"""

from conftest import TEST_API_TOKEN, TEST_WEBHOOK_SECRET, requires_claude, requires_llm
from livekit.agents import AgentSession

from voice_agent.agent import MorningBriefAgent
from voice_agent.backends import ClaudeSkillClient, InternalApi
from voice_agent.config import PROJECT_ROOT, settings
from voice_agent.knowledge_base import KnowledgeBase
from voice_agent.providers import build_llm

pytestmark = requires_llm


def make_agent(services) -> MorningBriefAgent:
    return MorningBriefAgent(
        instructions=settings.prompt_file.read_text(),
        knowledge_base=KnowledgeBase.load(PROJECT_ROOT / "knowledge_base"),
        internal_api=InternalApi(services.api_url, TEST_API_TOKEN),
        claude=ClaudeSkillClient(services.webhook_url, TEST_WEBHOOK_SECRET, timeout=40),
        claude_skill_name="ticket_triage_summary",
    )


async def ask(services, text: str):
    async with build_llm(settings) as llm, AgentSession(llm=llm) as session:
        await session.start(make_agent(services))
        result = await session.run(user_input=text)
        for ev in result.events:
            print(f"  {ev.type}: {getattr(ev, 'item', ev)}")
        return result


async def test_brief_uses_internal_api(services):
    result = await ask(services, "Give me my morning brief.")
    result.expect.contains_function_call(name="get_morning_brief")
    result.expect.contains_message(role="assistant")


async def test_policy_question_uses_knowledge_base(services):
    result = await ask(services, "What's our refund policy on annual maintenance plans?")
    result.expect.contains_function_call(name="search_knowledge_base")
    reply = result.expect.contains_message(role="assistant").event().item.text_content
    assert "60" in reply or "sixty" in reply.lower()


@requires_claude
async def test_triage_goes_to_claude_skill(services):
    result = await ask(services, "Ask Claude which tickets I should tackle first.")
    result.expect.contains_function_call(name="ask_claude_skill")
    result.expect.contains_message(role="assistant")
