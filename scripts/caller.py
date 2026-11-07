"""A scripted caller for testing and recording calls without a human.

Joins a room exactly like the web page (token from /api/token), speaks
synthesized questions into the call and records both sides on one timeline:
what the caller sends, what the agent sends back, the agent's captions and its
state changes. Used by e2e_call.py (checks) and record_demo.py (demo video).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
import urllib.request
from array import array
from dataclasses import dataclass, field
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
LOUD_RMS = 300  # int16 RMS above which a 10 ms window counts as speech

AGENT = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
t0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - t0:6.1f}s] {msg}", flush=True)


def rms(samples: array) -> float:
    return math.sqrt(sum(s * s for s in samples) / max(len(samples), 1))


@dataclass
class Question:
    """What the caller says. A part followed by a pause imitates someone who stops
    to think mid-sentence."""

    parts: list[tuple[str, float]]  # (text, seconds of silence after it)

    @property
    def text(self) -> str:
        return " ".join(p for p, _ in self.parts)


@dataclass
class Caption:
    speaker: str  # "Caller" or "Agent"
    text: str
    start: float  # seconds on the call timeline
    end: float


@dataclass
class Turn:
    question: Question
    start: float = 0.0  # when the caller started speaking
    end: float = 0.0  # when the caller finished
    agent_lines: list[str] = field(default_factory=list)
    early_states: list[str] = field(default_factory=list)  # agent states while caller spoke


async def synthesize(questions: list[Question]) -> list[list[array]]:
    """Each question's parts as int16 sample arrays, in a voice unlike the agent's."""
    tts = google.beta.GeminiTTS(
        voice_name="Puck",
        instructions="Say this like a relaxed American manager asking a quick question",
    )
    clips = []
    for q in questions:
        parts = []
        for text, _ in q.parts:
            async with tts.synthesize(text) as stream:
                pcm = array("h")
                async for ev in stream:
                    pcm.extend(array("h", bytes(ev.frame.data)))
            parts.append(_trim_silence(pcm))
        clips.append(parts)
    return clips


def _trim_silence(pcm: array, keep: int = FRAME * 5) -> array:
    """Cut TTS lead-in/tail silence so scripted pauses are exactly as long as stated."""
    loud = [i for i in range(0, len(pcm) - FRAME, FRAME) if rms(pcm[i : i + FRAME]) > LOUD_RMS]
    if not loud:
        return pcm
    return pcm[max(0, loud[0] - keep) : min(len(pcm), loud[-1] + FRAME + keep)]


def _bypass_proxy_for_local(url: str) -> None:
    # The LiveKit SDK uses HTTPS_PROXY but ignores NO_PROXY; reach a local server directly.
    if urllib.request.proxy_bypass(urlparse(url).hostname or ""):
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            os.environ.pop(var, None)


class ScriptedCall:
    def __init__(self) -> None:
        self.room = rtc.Room()
        self.agent: rtc.RemoteParticipant | None = None
        self.agent_joined = asyncio.Event()
        # one timeline, in samples since the caller's mic was published
        self.caller_pcm = array("h")
        self.agent_pcm = array("h")
        self.agent_offset = 0  # caller-timeline sample where agent_pcm[0] belongs
        self.captions: list[Caption] = []
        self.states: list[tuple[float, str]] = []
        self.last_activity = 0.0
        # small queue so "samples sent" tracks wall-clock time closely
        self.source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=100)
        self._speaking = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

        self.room.on("participant_connected", self._on_participant)
        self.room.on("track_subscribed", self._on_track)
        self.room.on("participant_attributes_changed", self._on_attributes)
        self.room.register_text_stream_handler("lk.transcription", self._on_transcript)

    # -- timeline -----------------------------------------------------------
    def now(self) -> float:
        return len(self.caller_pcm) / SAMPLE_RATE

    @property
    def agent_state(self) -> str:
        return self.agent.attributes.get("lk.agent.state", "") if self.agent else ""

    def mixed(self) -> array:
        """Caller and agent audio mixed on the shared timeline."""
        n = max(len(self.caller_pcm), self.agent_offset + len(self.agent_pcm))
        out = array("h", bytes(2 * n))
        for i, s in enumerate(self.caller_pcm):
            out[i] = s
        for i, s in enumerate(self.agent_pcm):
            j = self.agent_offset + i
            out[j] = max(-32768, min(32767, out[j] + s))
        return out

    def first_agent_sound_after(self, t: float) -> float | None:
        start = max(0, int(t * SAMPLE_RATE) - self.agent_offset)
        for i in range(start - start % FRAME, len(self.agent_pcm) - FRAME, FRAME):
            if rms(self.agent_pcm[i : i + FRAME]) > LOUD_RMS:
                return (self.agent_offset + i) / SAMPLE_RATE
        return None

    # -- room events --------------------------------------------------------
    def _on_participant(self, p: rtc.RemoteParticipant) -> None:
        if p.kind == AGENT and not self.agent_joined.is_set():
            self.agent = p
            log(f"agent joined: {p.identity}")
            self.agent_joined.set()

    def _on_attributes(self, changed: dict[str, str], p: rtc.Participant) -> None:
        if state := changed.get("lk.agent.state"):
            self.states.append((self.now(), state))
            self.last_activity = time.monotonic()
            log(f"agent state -> {state}")

    def _on_track(self, track: rtc.Track, pub, p: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and p.kind == AGENT:
            self._tasks.append(asyncio.create_task(self._record_agent(track)))

    async def _record_agent(self, track: rtc.Track) -> None:
        first = True
        async for ev in rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1):
            if first:
                self.agent_offset = len(self.caller_pcm)
                first = False
            self.agent_pcm.extend(array("h", bytes(ev.frame.data)))

    def _on_transcript(self, reader: rtc.TextStreamReader, identity: str) -> None:
        if identity == self.room.local_participant.identity:
            return  # our own words as the agent's STT heard them; not needed here
        start = self.now()

        async def read() -> None:
            text = (await reader.read_all()).strip()
            if text:
                self.captions.append(Caption("Agent", text, start, self.now()))
                self.last_activity = time.monotonic()
                log(f"  agent: {text}")

        self._tasks.append(asyncio.create_task(read()))

    # -- actions ------------------------------------------------------------
    async def connect(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{WEB_URL}/api/token", timeout=10)
            resp.raise_for_status()
            info = resp.json()
        log(f"token ok, room {info['roomName']}")
        _bypass_proxy_for_local(info["serverUrl"])
        await self.room.connect(info["serverUrl"], info["participantToken"])
        track = rtc.LocalAudioTrack.create_audio_track("mic", self.source)
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(track, opts)
        self._tasks.append(asyncio.create_task(self._silence()))
        for p in self.room.remote_participants.values():  # agent may already be here
            self._on_participant(p)

    async def _push(self, pcm: array) -> None:
        for i in range(0, len(pcm), FRAME):
            chunk = pcm[i : i + FRAME]
            if len(chunk) < FRAME:
                chunk.extend(array("h", bytes(2 * (FRAME - len(chunk)))))
            await self.source.capture_frame(
                rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, FRAME)
            )
            self.caller_pcm.extend(chunk)

    async def _silence(self) -> None:
        """Keep the mic track alive with silence, like a real microphone."""
        silent = array("h", bytes(2 * FRAME))
        while True:
            async with self._speaking:
                await self._push(silent)

    async def ask(self, question: Question, parts: list[array], quiet: float = 3.0) -> Turn:
        log(f"caller says: {question.text!r}")
        turn = Turn(question)
        before = len([c for c in self.captions if c.speaker == "Agent"])
        async with self._speaking:
            turn.start = self.now()
            states_before = len(self.states)
            for pcm, (_, pause) in zip(parts, question.parts):
                await self._push(pcm)
                await self._push(array("h", bytes(2 * int(pause * SAMPLE_RATE))))
            turn.end = self.now()
            turn.early_states = [s for _, s in self.states[states_before:]]
        self.captions.append(Caption("Caller", question.text, turn.start, turn.end))
        await self.wait_for_reply(after=before, quiet=quiet)
        turn.agent_lines = [c.text for c in self.captions if c.speaker == "Agent"][before:]
        return turn

    async def wait_for_reply(self, after: int, quiet: float = 3.0, timeout: float = 90.0) -> None:
        """Wait until the agent said something new and has been listening for `quiet` s."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            replied = len([c for c in self.captions if c.speaker == "Agent"]) > after
            idle = time.monotonic() - self.last_activity
            if replied and self.agent_state == "listening" and idle >= quiet:
                return
            await asyncio.sleep(0.2)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.room.disconnect()
