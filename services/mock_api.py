"""Mock "internal API": the stand-in for the client's real systems.

All data is fake and generated relative to today, so the brief always looks
current. Only GET endpoints exist: the agent has read-only access.

    uv run uvicorn services.mock_api:app --port 8001
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()

app = FastAPI(title="Acme internal API (mock)", version="1.0")

Priority = Literal["urgent", "high", "normal", "low"]


def require_token(authorization: str = Header(default="")) -> None:
    if API_TOKEN and authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid token")


def _now() -> datetime:
    return datetime.now().astimezone()


TICKETS = [
    # (id, priority, status, customer, title, assignee, hours since opened)
    ("T-1042", "urgent", "open", "Riverside Clinic", "Walk-in vaccine fridge not holding temperature", None, 2),
    ("T-1039", "urgent", "open", "Maple Street Bakery", "Rooftop unit leaking water into the kitchen", "Sam Patel", 5),
    ("T-1037", "high", "open", "Northgate Library", "Thermostat fault keeps coming back after repair", "Lee Moreno", 20),
    ("T-1035", "high", "open", "Harbor Fitness", "Invoice dispute: callout billed twice", "Billing team", 26),
    ("T-1031", "normal", "open", "Pine Ridge Apartments", "Book annual maintenance for 12 units", None, 50),
    ("T-1028", "normal", "open", "Oak Lane Dental", "Quote request for a new heat pump", "Lee Moreno", 70),
    ("T-1025", "low", "open", "Lakeside Florist", "Update site contact details", None, 96),
    ("T-1019", "high", "closed", "Hilltop Cafe", "Display cooler compressor replaced", "Sam Patel", 120),
]


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/v1/calendar/today", dependencies=[Depends(require_token)])
def calendar_today() -> dict:
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)

    def at(h: int, m: int = 0) -> str:
        return (today + timedelta(hours=h, minutes=m)).isoformat(timespec="minutes")

    return {
        "date": today.strftime("%A, %B %-d"),
        "events": [
            {"start": at(9), "end": at(9, 30), "title": "Weekly ops stand-up", "location": "Video call"},
            {"start": at(11), "end": at(11, 45), "title": "Contract renewal call with Riverside Clinic", "location": "Phone"},
            {"start": at(13, 30), "end": at(14, 30), "title": "Ride-along with new technician", "location": "Harbor Depot"},
            {"start": at(16), "end": at(16, 30), "title": "Interview: field technician candidate", "location": "North Depot"},
        ],
    }


@app.get("/v1/tickets", dependencies=[Depends(require_token)])
def tickets(status: Literal["open", "closed", "all"] = "open", priority: Priority | None = None) -> dict:
    now = _now()
    rows = [
        {
            "id": tid,
            "priority": prio,
            "status": st,
            "customer": customer,
            "title": title,
            "assignee": assignee or "unassigned",
            "opened_at": (now - timedelta(hours=age)).isoformat(timespec="minutes"),
        }
        for tid, prio, st, customer, title, assignee, age in TICKETS
        if (status == "all" or st == status) and (priority is None or prio == priority)
    ]
    return {"tickets": rows}


@app.get("/v1/sales/summary", dependencies=[Depends(require_token)])
def sales_summary() -> dict:
    return {
        "currency": "USD",
        "yesterday": {"revenue": 18420, "jobs_completed": 23, "new_maintenance_plans": 3},
        "month_to_date": {"revenue": 96300, "target": 150000, "percent_of_target": 64},
        "top_service_yesterday": "Emergency callouts",
    }
