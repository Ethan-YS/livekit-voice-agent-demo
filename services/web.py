"""Serves the call page and mints LiveKit room tokens.

Each "Start call" gets a fresh room. The token carries a room configuration that
dispatches the agent (AGENT_NAME) into that room, so the agent joins as soon as
the caller connects.

    uv run uvicorn services.web:app --port 8000
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from livekit import api

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
# The URL the *browser* connects to. Defaults to LIVEKIT_URL (fine for LiveKit Cloud).
LIVEKIT_PUBLIC_URL = os.environ.get("LIVEKIT_PUBLIC_URL") or os.environ.get("LIVEKIT_URL", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "morning-brief")
# Optional shared code so a public page can't be used by anyone who finds the URL.
WEB_ACCESS_CODE = os.environ.get("WEB_ACCESS_CODE", "").strip()

app = FastAPI(title="Voice agent web client", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/token")
def create_token(x_access_code: str = Header(default="")) -> dict:
    if WEB_ACCESS_CODE and not secrets.compare_digest(
        x_access_code.encode(), WEB_ACCESS_CODE.encode()
    ):
        raise HTTPException(status_code=401, detail="wrong access code")
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_PUBLIC_URL):
        raise HTTPException(status_code=500, detail="LiveKit settings missing on the server")

    room = f"brief-{secrets.token_hex(4)}"
    identity = f"caller-{secrets.token_hex(3)}"
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name("Caller")
        .with_ttl(timedelta(minutes=15))
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)])
        )
        .to_jwt()
    )
    return {"serverUrl": LIVEKIT_PUBLIC_URL, "roomName": room, "participantToken": token}
