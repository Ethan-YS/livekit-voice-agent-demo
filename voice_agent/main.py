"""LiveKit worker entrypoint.

    uv run python -m voice_agent dev     # local development (auto-reload, debug logs)
    uv run python -m voice_agent start   # production (what systemd runs)

The worker registers with LiveKit (LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)
under AGENT_NAME and is dispatched into a room whenever a caller's token asks for
that agent (see services/web.py).
"""

from __future__ import annotations

import logging
import os
import urllib.request
from urllib.parse import urlparse

from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, cli
from livekit.plugins import silero

from .agent import MorningBriefAgent
from .backends import ClaudeSkillClient, InternalApi
from .config import settings
from .knowledge_base import KnowledgeBase
from .providers import build_llm, build_stt, build_tts

logger = logging.getLogger("voice-agent")


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

    session = AgentSession(
        stt=build_stt(settings),
        llm=build_llm(settings),
        tts=build_tts(settings),
        vad=ctx.proc.userdata["vad"],
        turn_handling={"turn_detection": settings.turn_detection},
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


def main() -> None:
    cli.run_app(server)


if __name__ == "__main__":
    main()
