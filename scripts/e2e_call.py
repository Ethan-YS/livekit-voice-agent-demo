"""End-to-end check without a human, against the running stack (./run_local.sh).

Joins a room exactly like the web page (token from /api/token), speaks a few
synthesized questions into the call, and prints what the agent hears and says.
Exits non-zero if the agent never joins, stays silent, or skips a question.

    uv run python scripts/e2e_call.py
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
import urllib.request
from array import array
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from livekit import rtc
from livekit.plugins import google

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("GOOGLE_GENAI_API_VERSION", "v1beta")

WEB_URL = os.environ.get("WEB_URL", "http://127.0.0.1:8000")
SAMPLE_RATE = 24000  # Gemini TTS output rate
FRAME = SAMPLE_RATE // 100  # 10 ms
QUESTIONS = [
    "Give me my morning brief, please.",
    "What is our refund policy for annual maintenance plans?",
    "Ask Claude which tickets I should tackle first.",
]
FILLER_HINT = "one moment"

t0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - t0:6.1f}s] {msg}", flush=True)


async def synthesize(tts: google.beta.GeminiTTS, text: str) -> list[rtc.AudioFrame]:
    """The caller's voice. Gemini TTS, so it sounds different from the agent."""
    async with tts.synthesize(text) as stream:
        return [ev.frame async for ev in stream]


def bypass_proxy_for_local_livekit(url: str) -> None:
    # The LiveKit client SDK uses HTTPS_PROXY but ignores NO_PROXY; a local dev
    # server must be reached directly.
    host = urlparse(url).hostname or ""
    if urllib.request.proxy_bypass(host):
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            os.environ.pop(var, None)


class Call:
    def __init__(self) -> None:
        self.room = rtc.Room()
        self.agent: rtc.RemoteParticipant | None = None
        self.agent_joined = asyncio.Event()
        self.agent_lines: list[str] = []
        self.user_lines: list[str] = []
        self.agent_audio_frames = 0
        self.agent_loud_frames = 0
        self.last_agent_activity = time.monotonic()
        self.source = rtc.AudioSource(SAMPLE_RATE, 1)
        self._speaking = asyncio.Lock()
        self._silence_task: asyncio.Task | None = None

        self.room.on("participant_connected", self._on_participant)
        self.room.on("track_subscribed", self._on_track)
        self.room.on("participant_attributes_changed", self._on_attributes)
        self.room.register_text_stream_handler("lk.transcription", self._on_transcript)

    @property
    def agent_state(self) -> str:
        return self.agent.attributes.get("lk.agent.state", "") if self.agent else ""

    def _on_participant(self, p: rtc.RemoteParticipant) -> None:
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            self.agent = p
            log(f"agent joined: {p.identity}")
            self.agent_joined.set()

    def _on_attributes(self, changed: dict[str, str], p: rtc.Participant) -> None:
        if "lk.agent.state" in changed:
            self.last_agent_activity = time.monotonic()
            log(f"agent state -> {changed['lk.agent.state']}")

    def _on_track(self, track: rtc.Track, pub, p: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            log("subscribed to the agent's audio track")
            asyncio.create_task(self._measure(track))

    async def _measure(self, track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1):
            samples = array("h", bytes(ev.frame.data))
            rms = math.sqrt(sum(s * s for s in samples) / max(len(samples), 1))
            self.agent_audio_frames += 1
            if rms > 300:
                self.agent_loud_frames += 1

    def _on_transcript(self, reader: rtc.TextStreamReader, identity: str) -> None:
        async def read() -> None:
            text = (await reader.read_all()).strip()
            if not text:
                return
            if identity == self.room.local_participant.identity:
                self.user_lines.append(text)
                log(f"  user (as heard by STT): {text}")
            else:
                self.agent_lines.append(text)
                self.last_agent_activity = time.monotonic()
                log(f"  agent: {text}")

        asyncio.create_task(read())

    async def connect(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{WEB_URL}/api/token", timeout=10)
            resp.raise_for_status()
            info = resp.json()
        log(f"token ok, room {info['roomName']}")
        bypass_proxy_for_local_livekit(info["serverUrl"])
        await self.room.connect(info["serverUrl"], info["participantToken"])
        track = rtc.LocalAudioTrack.create_audio_track("mic", self.source)
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(track, opts)
        self._silence_task = asyncio.create_task(self._silence())
        for p in self.room.remote_participants.values():  # agent may already be here
            self._on_participant(p)

    async def _silence(self) -> None:
        """Keep the mic track alive with silence, like a real microphone."""
        silent = rtc.AudioFrame(b"\0\0" * FRAME, SAMPLE_RATE, 1, FRAME)
        while True:
            async with self._speaking:
                await self.source.capture_frame(silent)

    async def say(self, frames: list[rtc.AudioFrame]) -> None:
        async with self._speaking:
            for f in frames:
                await self.source.capture_frame(f)

    async def wait_for_reply(self, after: int, quiet: float = 4.0, timeout: float = 90.0) -> list[str]:
        """Wait for the agent to say something new, then until it has been back to
        listening, with no new speech, for `quiet` seconds. Returns the new lines."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            idle = time.monotonic() - self.last_agent_activity
            replied = len(self.agent_lines) > after
            if replied and self.agent_state == "listening" and idle >= quiet:
                return self.agent_lines[after:]
            await asyncio.sleep(0.25)
        return self.agent_lines[after:]


async def main() -> int:
    log("synthesizing test questions...")
    tts = google.beta.GeminiTTS(voice_name="Puck")
    clips = [await synthesize(tts, q) for q in QUESTIONS]

    call = Call()
    await call.connect()
    await asyncio.wait_for(call.agent_joined.wait(), timeout=20)
    greeting = await call.wait_for_reply(after=0, quiet=2, timeout=30)

    replies: list[list[str]] = []
    for question, clip in zip(QUESTIONS, clips):
        log(f"user says: {question!r}")
        before = len(call.agent_lines)
        await call.say(clip)
        replies.append(await call.wait_for_reply(after=before))

    if call._silence_task:
        call._silence_task.cancel()
    await call.room.disconnect()

    checks = {
        "agent joined": call.agent is not None,
        "agent greeted": bool(greeting),
        "agent audio has sound": call.agent_loud_frames > 50,
        "every question answered": all(replies),
        "filler spoken during Claude call": any(FILLER_HINT in line.lower() for line in replies[-1]),
    }
    print()
    print(f"agent audio frames: {call.agent_audio_frames} ({call.agent_loud_frames} with sound)")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
