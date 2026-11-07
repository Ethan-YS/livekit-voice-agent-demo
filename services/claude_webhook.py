"""Stand-in for the webhook into an internal Claude setup.

The voice agent POSTs {"skill", "request"}; this service runs one predefined,
read-only skill with a Claude model and returns the result. Here the skill is
"ticket_triage_summary": it reads the open tickets from the internal API and
asks Claude for a short spoken triage, through the Anthropic API
(ANTHROPIC_API_KEY).

    uv run uvicorn services.claude_webhook:app --port 8002
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
INTERNAL_API_URL = os.environ.get("INTERNAL_API_URL", "http://127.0.0.1:8001").rstrip("/")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_SKILL_MODEL = os.environ.get("CLAUDE_SKILL_MODEL", "claude-sonnet-5-5")
# The skill result is read out loud, so a quick answer beats a long think.
CLAUDE_SKILL_EFFORT = os.environ.get("CLAUDE_SKILL_EFFORT", "low")

_claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=30) if ANTHROPIC_API_KEY else None

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# skill name -> internal API path it is allowed to read
SKILLS = {"ticket_triage_summary": "/v1/tickets?status=open"}

logger = logging.getLogger("claude-webhook")
app = FastAPI(title="Claude skill webhook (demo)", version="1.0")


class SkillRequest(BaseModel):
    skill: str
    request: str = Field(max_length=2000)
    request_id: str | None = None


class SkillResult(BaseModel):
    request_id: str | None
    skill: str
    result: str
    model: str
    duration_ms: int


@app.get("/health")
def health() -> dict:
    return {"ok": True, "skills": sorted(SKILLS)}


@app.post("/skills/run", response_model=SkillResult)
async def run_skill(body: SkillRequest, authorization: str = Header(default="")) -> SkillResult:
    expected = f"Bearer {WEBHOOK_SECRET}".encode()
    if WEBHOOK_SECRET and not secrets.compare_digest(authorization.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    if body.skill not in SKILLS:
        raise HTTPException(status_code=404, detail=f"unknown skill {body.skill!r}")

    started = time.monotonic()
    data = await _read_internal(SKILLS[body.skill])
    prompt = (SKILLS_DIR / f"{body.skill}.md").read_text(encoding="utf-8")
    result = await _ask_claude(
        system=prompt,
        user=f"Question: {body.request}\n\nData (JSON):\n{json.dumps(data)}",
    )
    return SkillResult(
        request_id=body.request_id,
        skill=body.skill,
        result=result,
        model=CLAUDE_SKILL_MODEL,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def _read_internal(path: str) -> dict:
    headers = {"Authorization": f"Bearer {INTERNAL_API_TOKEN}"} if INTERNAL_API_TOKEN else {}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=5) as client:
            resp = await client.get(f"{INTERNAL_API_URL}{path}")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        logger.warning("internal API read failed: %r", e)
        raise HTTPException(status_code=502, detail="internal API unavailable") from e


async def _ask_claude(*, system: str, user: str) -> str:
    if _claude is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set")
    try:
        response = await _claude.beta.messages.create(
            model=CLAUDE_SKILL_MODEL,
            # Thinking counts toward max_tokens; leave room for the answer after it.
            max_tokens=4000,
            output_config={"effort": CLAUDE_SKILL_EFFORT},
            system=system,
            messages=[{"role": "user", "content": user}],
            # If a safety classifier declines, the API retries on Anthropic's recommended
            # fallback model inside the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        logger.warning("Claude call failed: HTTP %s %s", e.status_code, e.message)
        raise HTTPException(status_code=502, detail="Claude call failed") from e
    except anthropic.APIConnectionError as e:
        logger.warning("Claude call failed: %r", e)
        raise HTTPException(status_code=502, detail="Claude call failed") from e

    if response.stop_reason == "refusal":
        logger.warning("Claude declined the request: %s", response.stop_details)
        raise HTTPException(status_code=502, detail="Claude declined the request")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        logger.warning("Claude returned no text (stop_reason=%s)", response.stop_reason)
        raise HTTPException(status_code=502, detail="Claude returned no answer")
    return text
