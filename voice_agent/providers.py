"""Factories for the STT -> LLM -> TTS pipeline.

Swapping a provider means changing an env var (see `.env.example`). To add a
new provider, install its LiveKit plugin (`uv add "livekit-agents[deepgram]"`)
and add a branch below.
"""

from __future__ import annotations

from livekit.agents import llm, stt, tts
from livekit.plugins import assemblyai, google, openai

from .config import Settings


def build_stt(s: Settings) -> stt.STT:
    if s.stt_provider == "assemblyai":
        # AssemblyAI streaming STT; its end-of-turn model also drives turn detection
        # when TURN_DETECTION=stt.
        return assemblyai.STT(**({"speech_model": s.stt_model} if s.stt_model else {}))
    raise ValueError(f"Unknown STT_PROVIDER: {s.stt_provider!r}")


def build_llm(s: Settings) -> llm.LLM:
    # The OpenAI plugin talks to any OpenAI-compatible endpoint: OpenRouter (default),
    # OpenAI, Groq, a self-hosted vLLM, ...
    if not s.llm_api_key:
        raise ValueError("Set LLM_API_KEY (or OPENROUTER_API_KEY) in .env")
    return openai.LLM(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature,
    )


def build_tts(s: Settings) -> tts.TTS:
    if s.tts_provider == "assemblyai":
        # Streaming TTS over a pooled websocket: lowest time-to-first-audio.
        kwargs = {}
        if s.tts_voice:
            kwargs["voice"] = s.tts_voice
        if s.tts_model:
            kwargs["model"] = s.tts_model
        return assemblyai.TTS(**kwargs)
    if s.tts_provider == "gemini":
        # Gemini TTS (GOOGLE_API_KEY). Not streaming: each sentence is synthesized
        # whole, so the first word arrives ~1.5 s later than with AssemblyAI.
        return google.beta.GeminiTTS(
            voice_name=s.tts_voice or "Kore",
            **({"model": s.tts_model} if s.tts_model else {}),
        )
    raise ValueError(f"Unknown TTS_PROVIDER: {s.tts_provider!r}")
