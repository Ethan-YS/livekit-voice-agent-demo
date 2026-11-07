"""Clients for the systems the agent's tools read from.

Kept free of LiveKit types so they can be tested (and reused) on their own.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger("voice-agent.backends")


class BackendError(Exception):
    """A backend call failed; the message is safe to show to the LLM."""


class InternalApi:
    """Read-only client for the internal REST API (only GET requests)."""

    def __init__(self, base_url: str, token: str = "", timeout: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout

    async def _get(self, client: httpx.AsyncClient, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await client.get(f"{self._base_url}{path}", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("internal API GET %s failed: %r", path, e)
            raise BackendError("The internal system is not reachable right now.") from e
        return resp.json()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=self._timeout)

    async def morning_brief(self) -> dict[str, Any]:
        """Today's calendar, open tickets and sales numbers, fetched in parallel."""
        async with self._client() as client:
            calendar, tickets, sales = await asyncio.gather(
                self._get(client, "/v1/calendar/today"),
                self._get(client, "/v1/tickets", status="open"),
                self._get(client, "/v1/sales/summary"),
            )
        by_priority: dict[str, int] = {}
        for t in tickets["tickets"]:
            by_priority[t["priority"]] = by_priority.get(t["priority"], 0) + 1
        return {
            "date": calendar["date"],
            "calendar": calendar["events"],
            "open_tickets": {
                "total": len(tickets["tickets"]),
                "by_priority": by_priority,
                "urgent": [t for t in tickets["tickets"] if t["priority"] == "urgent"],
            },
            "sales": sales,
        }

    async def open_tickets(self, priority: str | None = None) -> list[dict[str, Any]]:
        async with self._client() as client:
            data = await self._get(client, "/v1/tickets", status="open", priority=priority)
        return data["tickets"]


class ClaudeSkillClient:
    """Calls the webhook in front of the internal Claude setup.

    The agent sends a request, Claude runs one predefined read-only skill on the
    other side, and the result comes back in the HTTP response.
    """

    def __init__(self, url: str, secret: str = "", timeout: float = 25.0) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        self._timeout = timeout

    async def run(self, skill: str, request: str) -> str:
        payload = {"skill": skill, "request": request, "request_id": uuid.uuid4().hex}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=self._timeout) as client:
                resp = await client.post(self._url, json=payload)
                resp.raise_for_status()
        except httpx.TimeoutException as e:
            logger.warning("Claude skill %s timed out after %.0fs", skill, self._timeout)
            raise BackendError(
                "The Claude assistant did not answer in time. Please try again in a minute."
            ) from e
        except httpx.HTTPError as e:
            logger.warning("Claude skill %s failed: %r", skill, e)
            raise BackendError("The Claude assistant is not available right now.") from e
        logger.info("Claude skill %s answered in %.1fs", skill, time.monotonic() - started)
        return resp.json()["result"]
