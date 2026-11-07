"""Record the demo video: a scripted caller talks to the running agent.

    ./run_local.sh                               # in another terminal
    uv run python scripts/record_demo.py         # -> demo/morning-brief-demo.{wav,mp4}

Both sides of the call are recorded on one timeline. Each video frame is the
call page plus a caption of who is speaking, rendered with headless Chrome, and
ffmpeg joins the frames with the audio. Needs ffmpeg and Google Chrome.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from caller import SAMPLE_RATE, Caption, Question, ScriptedCall, log, synthesize

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "demo"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
QUESTIONS = [
    Question([("Give me my morning brief.", 0)]),
    Question([("What's our refund policy for annual plans?", 0)]),
    Question([("Ask Claude which tickets I should tackle first.", 0)]),
]
LEAD_IN = 0.8  # seconds kept before the caller's first question
TAIL = 1.2  # seconds kept after the last words


async def record() -> tuple[ScriptedCall, float]:
    log("synthesizing caller audio...")
    clips = await synthesize(QUESTIONS)
    call = ScriptedCall()
    await call.connect()
    await asyncio.wait_for(call.agent_joined.wait(), timeout=20)
    await call.wait_for_reply(after=0, quiet=1.0, timeout=30)
    for question, clip in zip(QUESTIONS, clips):
        await call.ask(question, clip, quiet=1.0)  # reply about a second after the agent
    await call.close()
    first_question = min(c.start for c in call.captions if c.speaker == "Caller")
    return call, max(0.0, first_question - LEAD_IN)


def write_wav(call: ScriptedCall, path: Path, start: float) -> float:
    end = max(c.end for c in call.captions) + TAIL
    pcm = call.mixed()[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return len(pcm) / SAMPLE_RATE


def caption_chunks(captions: list[Caption], start: float) -> list[Caption]:
    """Split long agent turns into sentence-sized captions spread over the turn."""
    out = []
    for c in sorted(captions, key=lambda c: c.start):
        sentences = [s for s in re.split(r"(?<=[.?!])\s+", c.text.strip()) if s]
        chunks, cur = [], ""
        for s in sentences:  # pair short sentences so captions don't flicker
            cur = f"{cur} {s}".strip()
            if len(cur) > 70:
                chunks.append(cur)
                cur = ""
        if cur:
            chunks.append(cur)
        total = sum(len(x) for x in chunks)
        t = c.start - start
        for chunk in chunks:
            d = (c.end - c.start) * len(chunk) / total
            out.append(Caption(c.speaker, chunk, t, t + d))
            t += d
    return out


def state_at(call: ScriptedCall, t: float) -> str:
    state = "listening"
    for ts, s in call.states:
        if ts <= t:
            state = s
    return state


def render_frames(call: ScriptedCall, start: float, duration: float, work: Path) -> list[tuple[Path, float]]:
    """One PNG per change of caption or agent state, with how long it stays on screen."""
    captions = caption_chunks(call.captions, start)
    phone = work / "phone.png"
    _render_phone(phone)

    cuts = {0.0, duration}
    cuts.update(t for c in captions for t in (c.start, c.end) if 0 <= t <= duration)
    cuts.update(ts - start for ts, _ in call.states if 0 <= ts - start <= duration)
    times = sorted(cuts)

    frames, last_key, last_path = [], None, None
    for i, (a, b) in enumerate(zip(times, times[1:])):
        if b - a < 0.05:
            continue
        mid = (a + b) / 2
        current = next((c for c in captions if c.start <= mid < c.end), None)
        status = {"thinking": "Thinking…", "speaking": "Speaking"}.get(
            state_at(call, start + mid), "Listening"
        )
        if current and current.speaker == "Caller":
            status = "Listening"
        key = (current.speaker if current else "", current.text if current else "", status)
        if key != last_key:
            last_path = work / f"frame_{i:03d}.png"
            _render(_frame_html(phone, key), last_path)
            frames.append((last_path, b - a))
            last_key = key
        else:
            path, d = frames[-1]
            frames[-1] = (path, d + b - a)
    return frames


def _render(html_text: str, png: Path, size: str = "1280,720") -> None:
    page = png.with_suffix(".html")
    page.write_text(html_text, encoding="utf-8")
    subprocess.run(
        [CHROME, "--headless=new", f"--user-data-dir={png.parent / 'chrome-profile'}",
         "--disable-gpu", "--hide-scrollbars", f"--window-size={size}",
         "--force-device-scale-factor=1", f"--screenshot={png}", page.as_uri()],
        check=True, capture_output=True, timeout=60,
    )


def _render_phone(png: Path) -> None:
    """The real call page (web/index.html) at phone size, set to its in-call look."""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    in_call = """<script>
      setCallUi(true);
      setStatus("In call");
      document.getElementById("meter").style.width = "45%";
      document.getElementById("agentText").textContent = "…";
      document.getElementById("userText").textContent = "…";
    </script></body>"""
    # drop the CDN script (no network needed) and stub the one name the page reads at load
    page = re.sub(r'<script src="[^"]+"[^>]*></script>', "", page)
    page = page.replace("const { Room, RoomEvent, Track, ParticipantKind } = LivekitClient;",
                        "const { Room, RoomEvent, Track, ParticipantKind } = window.LivekitClient || {};")
    _render(page.replace("</body>", in_call, 1), png, size="500,1082")  # headless minimum width is ~500


def _frame_html(phone: Path, key: tuple[str, str, str]) -> str:
    speaker, text, status = key
    caption = (
        f'<div class="cap"><b class="{speaker.lower()}">{speaker}</b>{html.escape(text)}</div>'
        if text else '<div class="cap"></div>'
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ margin:0; width:1280px; height:720px; background:#0f1115; color:#f2f4f7;
             font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; overflow:hidden; }}
      .left {{ position:absolute; left:64px; top:56px; width:700px; }}
      h1 {{ font-size:40px; margin:0 0 8px; }}
      .sub {{ color:#9aa3b2; font-size:22px; }}
      .pipe {{ margin-top:28px; color:#c9d1dc; font-size:18px; line-height:1.7; }}
      .pill {{ display:inline-block; margin-top:28px; padding:8px 18px; border-radius:999px;
              background:#1d2129; font-size:20px; color:{'#3ddc97' if status == 'Speaking' else '#c9d1dc'}; }}
      .cap {{ position:absolute; left:64px; top:390px; width:700px; font-size:30px; line-height:1.35; }}
      .cap b {{ display:block; font-size:18px; letter-spacing:.08em; text-transform:uppercase; margin-bottom:8px; }}
      .agent {{ color:#3ddc97; }} .caller {{ color:#7cb4ff; }}
      .phone {{ position:absolute; right:90px; top:40px; height:640px; border-radius:36px;
               border:8px solid #2a2f38; }}
      .note {{ position:absolute; left:64px; bottom:24px; font-size:15px; color:#7d8696; }}
    </style></head><body>
      <div class="left">
        <h1>Morning brief voice agent</h1>
        <div class="sub">LiveKit Agents demo</div>
        <div class="pipe">AssemblyAI speech-to-text &rarr; Claude Haiku &rarr; AssemblyAI voice<br>
          Tools: internal API &middot; knowledge base &middot; Claude skill webhook</div>
        <div class="pill">Agent: {status}</div>
      </div>
      {caption}
      <img class="phone" src="{phone.as_uri()}">
      <div class="note">Scripted caller (TTS) &middot; demo data</div>
    </body></html>"""


def build_mp4(frames: list[tuple[Path, float]], wav: Path, mp4: Path, work: Path) -> None:
    listing = work / "frames.txt"
    lines = []
    for path, d in frames:
        lines += [f"file '{path}'", f"duration {d:.3f}"]
    lines.append(f"file '{frames[-1][0]}'")  # concat demuxer needs the last frame twice
    listing.write_text("\n".join(lines) + "\n")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-i", str(wav), "-r", "15", "-c:v", "libx264", "-tune", "stillimage", "-crf", "24",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest",
         "-movflags", "+faststart", str(mp4)],
        check=True,
    )


async def main() -> int:
    if not shutil.which("ffmpeg") or not Path(CHROME).exists():
        print("needs ffmpeg (brew install ffmpeg) and Google Chrome", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(exist_ok=True)
    call, start = await record()

    wav = OUT_DIR / "morning-brief-demo.wav"
    duration = write_wav(call, wav, start)
    (OUT_DIR / "captions.json").write_text(json.dumps(
        [c.__dict__ for c in caption_chunks(call.captions, start)], indent=1))
    log(f"wrote {wav.name} ({duration:.1f} s)")

    with tempfile.TemporaryDirectory() as tmp:
        frames = render_frames(call, start, duration, Path(tmp))
        log(f"rendered {len(frames)} frames")
        mp4 = OUT_DIR / "morning-brief-demo.mp4"
        build_mp4(frames, wav, mp4, Path(tmp))
    log(f"wrote {mp4.name} ({mp4.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
