"""End-to-end check without a human, against the running stack (./run_local.sh).

A scripted caller asks three questions, two of them with a mid-sentence pause
like a person thinking. Checks that the agent waits for the whole question,
answers each one, speaks the filler while the Claude skill runs, and prints the
response time (caller stops talking -> first agent audio).

    uv run python scripts/e2e_call.py
"""

from __future__ import annotations

import asyncio
import sys

from caller import Question, ScriptedCall, log, synthesize

QUESTIONS = [
    Question([("Give me my morning brief, please.", 0)]),
    Question([("What's our refund policy for", 0.9), ("annual maintenance plans?", 0)]),
    Question([("Ask Claude which tickets", 0.9), ("I should tackle first.", 0)]),
]
FILLER_HINT = "one moment"


async def main() -> int:
    log("synthesizing caller audio...")
    clips = await synthesize(QUESTIONS)

    call = ScriptedCall()
    await call.connect()
    await asyncio.wait_for(call.agent_joined.wait(), timeout=20)
    await call.wait_for_reply(after=0, quiet=1.5, timeout=30)
    greeted = any(c.speaker == "Agent" for c in call.captions)

    turns = [await call.ask(q, clip) for q, clip in zip(QUESTIONS, clips)]
    await call.close()

    print()
    for turn in turns:
        first = call.first_agent_sound_after(turn.end)
        latency = f"{first - turn.end:.1f} s" if first else "no audio"
        print(f"{turn.question.text!r}: first agent audio after {latency}")
    checks = {
        "agent greeted": greeted,
        "agent waited for complete questions": not any(
            s in ("thinking", "speaking") for t in turns for s in t.early_states
        ),
        "every question answered": all(t.agent_lines for t in turns),
        "filler spoken during Claude call": any(
            FILLER_HINT in line.lower() for line in turns[-1].agent_lines
        ),
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
