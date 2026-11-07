"""Stand-in for the webhook into an internal Claude setup.

The voice agent POSTs {"skill", "request"}; this service runs one predefined,
read-only skill with a Claude model and returns the result. Here the skill is
"ticket_triage_summary": it reads the open tickets from the internal API and
asks Claude for a short spoken triage. Claude is reached through OpenRouter's
OpenAI-compatible API; point CLAUDE_API_BASE at another gateway if needed.

    uv run uvicorn services.claude_webhook:app --port 8002
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
INTERNAL_API_URL = os.environ.get("INTERNAL_API_URL", "http://127.0.0.1:8001").rstrip("/")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()
CLAUDE_API_BASE = os.environ.get("CLAUDE_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
CLAUDE_API_KEY = (os.environ.get("CLAUDE_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")).strip()
CLAUDE_SKILL_MODEL = os.environ.get("CLAUDE_SKILL_MODEL", "anthropic/claude-sonnet-5.5")

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
    if not CLAUDE_API_KEY:
        raise HTTPException(status_code=500, detail="CLAUDE_API_KEY / OPENROUTER_API_KEY not set")
    payload = {
        "model": CLAUDE_SKILL_MODEL,
        # Newer Claude models always think first; keep it short (low latency) and leave
        # enough token budget for the answer after the thinking.
        "reasoning": {"effort": "low"},
        "max_tokens": 2000,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{CLAUDE_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {CLAUDE_API_KEY}"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError) as e:
        logger.warning("Claude call failed: %r", e)
        raise HTTPException(status_code=502, detail="Claude call failed") from e
