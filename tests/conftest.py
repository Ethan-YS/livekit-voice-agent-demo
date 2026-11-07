"""Test fixtures: start the mock internal API and the Claude webhook on free ports."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
TEST_API_TOKEN = "test-internal-token"
TEST_WEBHOOK_SECRET = "test-webhook-secret"

requires_claude = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs ANTHROPIC_API_KEY (real Claude call)",
)
# The voice agent's LLM: Claude by default, or an OpenAI-compatible endpoint
_llm_key = (
    os.environ.get("ANTHROPIC_API_KEY")
    if os.environ.get("LLM_PROVIDER", "anthropic") == "anthropic"
    else os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
)
requires_llm = pytest.mark.skipif(not _llm_key, reason="needs the LLM provider's API key")


@dataclass
class Services:
    api_url: str
    webhook_url: str


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(module: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"services.{module}:app", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_healthy(url: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="session")
def services() -> Iterator[Services]:
    api_port, webhook_port = _free_port(), _free_port()
    api_url = f"http://127.0.0.1:{api_port}"
    # Explicit values win over .env (load_dotenv never overrides existing variables).
    env = {
        **os.environ,
        "INTERNAL_API_TOKEN": TEST_API_TOKEN,
        "INTERNAL_API_URL": api_url,
        "WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
        "NO_PROXY": "127.0.0.1,localhost",
    }
    procs = [_start("mock_api", api_port, env), _start("claude_webhook", webhook_port, env)]
    try:
        _wait_healthy(f"{api_url}/health")
        _wait_healthy(f"http://127.0.0.1:{webhook_port}/health")
        yield Services(api_url=api_url, webhook_url=f"http://127.0.0.1:{webhook_port}/skills/run")
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=5)
