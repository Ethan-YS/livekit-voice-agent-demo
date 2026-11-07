"""Factories for the STT -> LLM -> TTS pipeline and turn detection.

Swapping a provider means changing an env var (see `.env.example`). To add a
new provider, install its LiveKit plugin (`uv add "livekit-agents[deepgram]"`)
and add a branch below.
"""

from __future__ import annotations

from livekit.agents import TurnHandlingOptions, inference, llm, stt, tts
from livekit.plugins import assemblyai, google, openai

from .config import Settings


def build_stt(s: Settings) -> stt.STT:
    if s.stt_provider == "assemblyai":
        kwargs: dict = {"keyterms_prompt": list(s.stt_keyterms)} if s.stt_keyterms else {}
        if s.stt_model:
            kwargs["model"] = s.stt_model
        if s.turn_detection == "stt":
            # AssemblyAI decides the end of the turn: make it wait for a real pause.
            kwargs["min_turn_silence"] = int(s.endpointing_min_delay * 1000)
            kwargs["max_turn_silence"] = int(s.endpointing_max_delay * 1000)
        return assemblyai.STT(**kwargs)
    raise ValueError(f"Unknown STT_PROVIDER: {s.stt_provider!r}")


def build_turn_handling(s: Settings) -> TurnHandlingOptions:
    # Answer after `min_delay` of silence when the caller sounds finished; wait up to
    # `max_delay` when they stopped mid-sentence (people pause to think).
    endpointing = {"min_delay": s.endpointing_min_delay, "max_delay": s.endpointing_max_delay}
    if s.turn_detection == "model":
        kwargs = {}
        if s.turn_unlikely_threshold:
            kwargs["unlikely_threshold"] = float(s.turn_unlikely_threshold)
        detector = inference.TurnDetector(version=s.turn_detector_version, **kwargs)
        return {"turn_detection": detector, "endpointing": endpointing}
    if s.turn_detection in ("stt", "vad"):
        return {"turn_detection": s.turn_detection, "endpointing": endpointing}
    raise ValueError(f"Unknown TURN_DETECTION: {s.turn_detection!r}")


def build_llm(s: Settings) -> llm.LLM:
    # The OpenAI plugin talks to any OpenAI-compatible endpoint: OpenRouter (default),
    # OpenAI, Groq, a self-hosted vLLM, ...
    if not s.llm_api_key:
        raise ValueError("Set LLM_API_KEY (or OPENROUTER_API_KEY) in .env")
    kwargs = {}
    if "openrouter.ai" in s.llm_base_url:
        # Route each request to the provider with the lowest current latency.
        kwargs["extra_body"] = {"provider": {"sort": "latency"}}
    return openai.LLM(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature,
        **kwargs,
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
