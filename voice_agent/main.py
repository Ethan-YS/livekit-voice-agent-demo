"""LiveKit worker entrypoint.

    uv run python -m voice_agent dev     # local development (auto-reload, debug logs)
    uv run python -m voice_agent start   # production (what systemd runs)

The worker registers with LiveKit (LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)
under AGENT_NAME and is dispatched into a room whenever a caller's token asks for
that agent (see services/web.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.request
from urllib.parse import urlparse

from livekit.agents import (
    APIConnectOptions,
    AgentServer,
    AgentSession,
    ChatContext,
    JobContext,
    JobProcess,
    SessionConnectOptions,
    cli,
)
from livekit.agents.llm import LLM
from livekit.plugins import silero

from .agent import MorningBriefAgent
from .backends import ClaudeSkillClient, InternalApi
from .config import settings
from .knowledge_base import KnowledgeBase
from .providers import build_llm, build_stt, build_tts, build_turn_handling

logger = logging.getLogger("voice-agent")
_background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks referenced


def _bypass_proxy_for_local_livekit() -> None:
    """The LiveKit SDKs read HTTPS_PROXY / HTTP_PROXY but ignore NO_PROXY. When
    LIVEKIT_URL is a local dev server, drop the proxy variables so it is reachable."""
    host = urlparse(os.environ.get("LIVEKIT_URL", "")).hostname or ""
    if host and urllib.request.proxy_bypass(host):
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            os.environ.pop(var, None)


_bypass_proxy_for_local_livekit()
server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    """Runs once per worker process, before any call: load the slow things here."""
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["kb"] = KnowledgeBase.load(settings.knowledge_base_dir)


server.setup_fnc = prewarm


@server.rtc_session(agent_name=settings.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    kb: KnowledgeBase = ctx.proc.userdata["kb"]
    logger.info("call started in room %s (%d KB sections)", ctx.room.name, len(kb.sections))

    llm = build_llm(settings)
    # Open the LLM connection while the greeting plays, so the first real question
    # doesn't pay for the TLS handshake and provider routing.
    warm_up = asyncio.create_task(_warm_up(llm))
    _background.add(warm_up)
    warm_up.add_done_callback(_background.discard)

    session = AgentSession(
        stt=build_stt(settings),
        llm=llm,
        tts=build_tts(settings),
        vad=ctx.proc.userdata["vad"],
        turn_handling=build_turn_handling(settings),
        conn_options=SessionConnectOptions(
            # Hosted APIs occasionally stall a request. Retry after a few seconds without
            # a byte instead of the 10 s default (normal: first token 1-3 s, first
            # audio about 0.4 s once text is sent).
            llm_conn_options=APIConnectOptions(max_retry=2, timeout=settings.llm_timeout),
            tts_conn_options=APIConnectOptions(max_retry=2, timeout=settings.tts_timeout),
        ),
    )

    agent = MorningBriefAgent(
        instructions=settings.prompt_file.read_text(encoding="utf-8"),
        knowledge_base=kb,
        internal_api=InternalApi(
            settings.internal_api_url,
            settings.internal_api_token,
            settings.internal_api_timeout,
        ),
        claude=ClaudeSkillClient(
            settings.claude_webhook_url,
            settings.webhook_secret,
            settings.webhook_timeout,
        ),
        claude_skill_name=settings.claude_skill_name,
    )

    await session.start(agent=agent, room=ctx.room)
    session.say(settings.greeting)


async def _warm_up(model: LLM) -> None:
    ctx = ChatContext.empty()
    ctx.add_message(role="user", content="Reply with one word: ready")
    try:
        async with model.chat(chat_ctx=ctx) as stream:
            async for _ in stream:
                break
    except Exception as e:  # a failed warm-up only costs latency, never the call
        logger.info("LLM warm-up failed: %r", e)


def main() -> None:
    cli.run_app(server)


if __name__ == "__main__":
    main()
