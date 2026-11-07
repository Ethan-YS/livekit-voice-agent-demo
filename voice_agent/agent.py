"""The voice agent: instructions plus the tools the LLM can call."""

from __future__ import annotations

import json
import logging
from typing import Literal

from livekit.agents import Agent, RunContext, ToolError, function_tool
from livekit.agents.voice import SpeechHandle

from .backends import BackendError, ClaudeSkillClient, InternalApi
from .knowledge_base import KnowledgeBase

logger = logging.getLogger("voice-agent")

# Spoken while the Claude skill runs: first right away, then once more if it is slow.
CLAUDE_FILLERS = (
    "Let me check that, one moment.",
    "Still working on it, thanks for waiting.",
)


class MorningBriefAgent(Agent):
    def __init__(
        self,
        *,
        instructions: str,
        knowledge_base: KnowledgeBase,
        internal_api: InternalApi,
        claude: ClaudeSkillClient,
        claude_skill_name: str,
    ) -> None:
        super().__init__(instructions=instructions)
        self._kb = knowledge_base
        self._api = internal_api
        self._claude = claude
        self._claude_skill_name = claude_skill_name

    @function_tool
    async def get_morning_brief(self, context: RunContext) -> str:
        """Get today's brief from the internal systems. Use for "my brief", "what's
        my day like", or any overview question. Speak only the `headline`; keep
        `details` (full calendar, all open tickets, sales) for follow-up questions."""
        logger.info("tool get_morning_brief")
        try:
            return json.dumps(await self._api.morning_brief())
        except BackendError as e:
            raise ToolError(str(e)) from e

    @function_tool
    async def get_open_tickets(
        self,
        context: RunContext,
        priority: Literal["urgent", "high", "normal", "low"] | None = None,
    ) -> str:
        """List open support tickets, optionally only one priority level.

        Args:
            priority: Only return tickets with this priority. Leave empty for all.
        """
        logger.info("tool get_open_tickets priority=%s", priority)
        try:
            return json.dumps({"tickets": await self._api.open_tickets(priority)})
        except BackendError as e:
            raise ToolError(str(e)) from e

    @function_tool
    async def search_knowledge_base(self, context: RunContext, query: str) -> str:
        """Search the company knowledge base (policies, services, pricing,
        procedures, contacts).

        Args:
            query: What to look for, in a few keywords, e.g. "refund annual plan".
        """
        logger.info("tool search_knowledge_base query=%r", query)
        hits = self._kb.search(query, k=3)
        if not hits:
            return "No matching entries in the knowledge base."
        return "\n\n".join(f"[{h.section.title}]\n{h.section.text}" for h in hits)

    @function_tool
    async def ask_claude_skill(self, context: RunContext, request: str) -> str:
        """Ask the internal Claude assistant to analyse the open tickets: a triage of
        what to tackle first, risks, and who should handle what. Takes several seconds.

        Args:
            request: The user's question, in their own words.
        """
        logger.info("tool ask_claude_skill request=%r", request)

        def filler(step: int) -> SpeechHandle | None:
            if step >= len(CLAUDE_FILLERS):
                return None
            # Spoken only: kept out of the chat history so the LLM's next turn still
            # follows the tool result (Claude models reject a trailing assistant message).
            return context.session.say(CLAUDE_FILLERS[step], add_to_chat_ctx=False)

        # with_filler speaks as soon as the agent is idle, and again after 8 s
        # if the webhook is still busy. It stops the moment the result is back.
        async with context.with_filler(filler, delay=0.2, interval=8, max_steps=2):
            try:
                return await self._claude.run(self._claude_skill_name, request)
            except BackendError as e:
                raise ToolError(str(e)) from e
