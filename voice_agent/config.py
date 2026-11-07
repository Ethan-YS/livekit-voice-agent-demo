"""All runtime settings, read from environment variables (or a local .env file).

Every knob a non-developer is likely to touch (models, voice, URLs, timeouts)
lives here, so changing the agent is a matter of editing `.env` and restarting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# Gemini TTS needs the v1beta Gemini API; the google-genai SDK defaults to v1.
os.environ.setdefault("GOOGLE_GENAI_API_VERSION", "v1beta")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # LiveKit worker registration (used by the LiveKit SDK itself)
    agent_name: str = _env("AGENT_NAME", "morning-brief")

    # Speech-to-text
    stt_provider: str = _env("STT_PROVIDER", "assemblyai")
    stt_model: str = _env("STT_MODEL")  # empty = provider default
    # Words the recognizer should expect (names, product terms), comma-separated
    stt_keyterms: tuple[str, ...] = tuple(
        t.strip()
        for t in _env(
            "STT_KEYTERMS",
            "Claude,morning brief,annual plan,annual maintenance plan,tickets,Riverside Clinic",
        ).split(",")
        if t.strip()
    )

    # Turn detection: when has the caller finished speaking?
    #   "model": LiveKit's end-of-turn model (local "v1-mini" runs offline; use
    #            TURN_DETECTOR_VERSION=v1 on LiveKit Cloud for the hosted model)
    #   "stt":   AssemblyAI's end-of-turn signal;  "vad": silence only
    turn_detection: str = _env("TURN_DETECTION", "model")
    turn_detector_version: str = _env("TURN_DETECTOR_VERSION", "v1-mini")
    # Below this end-of-turn probability the model treats the sentence as unfinished
    # and waits up to ENDPOINTING_MAX_DELAY. Empty = LiveKit's default for the language.
    turn_unlikely_threshold: str = _env("TURN_UNLIKELY_THRESHOLD")
    # Silence (s) before answering when the sentence sounds finished / unfinished
    endpointing_min_delay: float = float(_env("ENDPOINTING_MIN_DELAY", "1.2"))
    endpointing_max_delay: float = float(_env("ENDPOINTING_MAX_DELAY", "2.0"))

    # LLM: any OpenAI-compatible endpoint (OpenRouter by default)
    llm_base_url: str = _env("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_api_key: str = _env("LLM_API_KEY") or _env("OPENROUTER_API_KEY")
    llm_model: str = _env("LLM_MODEL", "anthropic/claude-haiku-5.5")
    llm_temperature: float = float(_env("LLM_TEMPERATURE", "0.4"))
    # Give up on a silent LLM request after this many seconds and retry
    llm_timeout: float = float(_env("LLM_TIMEOUT_SECONDS", "6"))

    # Text-to-speech
    tts_provider: str = _env("TTS_PROVIDER", "assemblyai")  # "assemblyai" or "gemini"
    tts_voice: str = _env("TTS_VOICE")  # empty = provider default
    tts_model: str = _env("TTS_MODEL")  # empty = provider default
    # Retry a TTS request that has produced no audio after this many seconds
    tts_timeout: float = float(_env("TTS_TIMEOUT_SECONDS", "2"))

    # Prompt and greeting
    prompt_file: Path = PROJECT_ROOT / _env("PROMPT_FILE", "voice_agent/prompt.md")
    greeting: str = _env(
        "GREETING", "Good morning! Want me to run through your morning brief?"
    )

    # Data sources the tools read from
    knowledge_base_dir: Path = PROJECT_ROOT / _env("KNOWLEDGE_BASE_DIR", "knowledge_base")
    internal_api_url: str = _env("INTERNAL_API_URL", "http://127.0.0.1:8001")
    internal_api_token: str = _env("INTERNAL_API_TOKEN")
    internal_api_timeout: float = float(_env("INTERNAL_API_TIMEOUT_SECONDS", "5"))

    # Webhook to the internal Claude setup
    claude_webhook_url: str = _env("CLAUDE_WEBHOOK_URL", "http://127.0.0.1:8002/skills/run")
    webhook_secret: str = _env("WEBHOOK_SECRET")
    webhook_timeout: float = float(_env("WEBHOOK_TIMEOUT_SECONDS", "25"))
    claude_skill_name: str = _env("CLAUDE_SKILL_NAME", "ticket_triage_summary")


settings = Settings()
