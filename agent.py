import asyncio
import csv
import os
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.protobuf.duration_pb2 import Duration
from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import deepgram, openai, silero

from scenarios import EXPECTED, build_instructions, get_scenario, require_caller_number

load_dotenv()
logging.basicConfig(level=logging.INFO)

# The assessment line. Hard-coded on purpose: every graded call must go here and nowhere else.
TARGET_NUMBER = "+18054398008"
SIP_IDENTITY = "patientprobe-receptionist"

# Two separate limits (both overridable from .env):
#   DIAL_TIMEOUT_SECONDS: how long we let the phone ring before giving up.
#   MAX_CALL_SECONDS:     how long an ANSWERED call may last. The clock starts at answer.
#   IDLE_TIMEOUT_SECONDS: hang up if nobody has said anything for this long (dead air,
#                         e.g. a transfer that goes silent). Real gaps seen so far: ~35s.
#   TRANSFER_GUARD_SECONDS: after the receptionist announces a transfer, refuse to hang up
#                         for this long, so the patient waits for someone to answer.
DIAL_TIMEOUT_SECONDS = int(os.getenv("DIAL_TIMEOUT_SECONDS", "30"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "360"))
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "60"))
TRANSFER_GUARD_SECONDS = int(os.getenv("TRANSFER_GUARD_SECONDS", "30"))

CALL_LOG = Path("call_log.csv")

# Tune these after listening to real calls, and note each change in the call log.
TURN_SETTINGS = {
    "endpointing": {"mode": "fixed", "min_delay": 0.6, "max_delay": 2.5},
    "interruption": {"enabled": True},
}


def job_config(ctx: JobContext) -> tuple[str, dict]:
    """Scenario and overrides come only from the dispatch metadata (see make_call.py)."""
    meta = json.loads(ctx.job.metadata) if ctx.job.metadata else {}
    scenario_id = meta.get("scenario_id")
    if not scenario_id:
        raise ValueError("No scenario_id in dispatch metadata. Start calls with make_call.py.")
    return scenario_id, meta.get("overrides", {})


class PatientAgent(Agent):
    def __init__(self, instructions: str, hang_up, transfer_pending):
        super().__init__(instructions=instructions)
        self._hang_up = hang_up
        self._transfer_pending = transfer_pending

    @function_tool
    async def end_call(self, context: RunContext):
        """Hang up the call. Use only after saying goodbye, once your goal is
        done or the receptionist clearly can't help."""
        if self._transfer_pending():
            # Don't hang up on a transfer: the model should wait for someone to answer.
            logging.warning("end_call blocked: transfer in progress")
            return (
                "The receptionist is transferring you. Do not hang up. Stay on the line "
                "and say hello when someone answers."
            )
        await context.wait_for_playout()  # let the goodbye finish first
        await self._hang_up("patient_ended_call")


async def entrypoint(ctx: JobContext):
    # --- validate everything BEFORE spending anything ---------------------------
    caller_number = require_caller_number()
    trunk_id = os.environ["SIP_OUTBOUND_TRUNK_ID"]
    scenario_id, overrides = job_config(ctx)
    scenario = get_scenario(scenario_id)
    # Raises for scenarios 02/03 if no confirmed slot was supplied.
    instructions = build_instructions(scenario_id, **overrides)

    logging.info("Starting scenario %s: %s", scenario_id, scenario["name"])

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path("recordings") / f"scenario_{scenario_id}" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    transcript_txt = output_dir / "transcript.txt"
    transcript_jsonl = output_dir / "transcript.jsonl"

    end: dict = {}  # filled in as the call progresses: reason, answered_at_seconds, ended_at
    stats = {"turns": 0}
    meta = {
        "scenario_id": scenario_id,
        "name": scenario["name"],
        "test_focus": scenario["test_focus"],
        "expected": EXPECTED[scenario_id],
        "overrides": overrides,
        "turn_settings": TURN_SETTINGS,
        "dial_timeout_seconds": DIAL_TIMEOUT_SECONDS,
        "max_call_seconds": MAX_CALL_SECONDS,
        "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
        "transfer_guard_seconds": TRANSFER_GUARD_SECONDS,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    def write_meta():
        (output_dir / "meta.json").write_text(
            json.dumps({**meta, **end}, indent=2), encoding="utf-8"
        )

    write_meta()

    # Transcript times are relative to this moment, which is also when the session
    # (and the recording) starts. The first receptionist line appears a few seconds in,
    # after the phone rings. `answered_at_seconds` in meta.json says when.
    start_time = time.monotonic()
    last_activity = {"t": time.monotonic()}  # bumped on every transcript line
    transfer_guard = {"until": 0.0}  # monotonic time until which hanging up is blocked

    def transfer_pending() -> bool:
        """True while a transfer the receptionist announced is still in progress. The
        window is set when they say 'transferring you' / 'connecting you' (an announcement,
        not an offer like 'should I transfer you?') and is NOT cleared by later lines."""
        return time.monotonic() < transfer_guard["until"]

    def save_turn(speaker: str, content: str):
        stats["turns"] += 1
        last_activity["t"] = time.monotonic()
        if speaker == "RECEPTIONIST":
            lowered = content.lower()
            if "transferring you" in lowered or "connecting you" in lowered:
                transfer_guard["until"] = time.monotonic() + TRANSFER_GUARD_SECONDS
                logging.info("Transfer detected: hang-up blocked for %ss", TRANSFER_GUARD_SECONDS)
        elapsed = time.monotonic() - start_time
        timestamp = f"{int(elapsed) // 60:02d}:{elapsed % 60:04.1f}"

        record = {
            "timestamp": timestamp,
            "elapsed_seconds": round(elapsed, 1),
            "speaker": speaker,
            "text": content,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

        with transcript_txt.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {speaker}: {content}\n")

        with transcript_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logging.info("[%s] %s: %s", timestamp, speaker, content)

    logging.info("Transcripts will be saved to %s", output_dir)

    # --- shared cleanup ---------------------------------------------------------
    background: set[asyncio.Task] = set()

    def spawn(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)
        return task

    async def hang_up(reason: str):
        """Record why, delete the room (which also drops any SIP call in it), shut down."""
        end.setdefault("reason", reason)
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        except Exception:
            logging.exception("delete_room failed")
        ctx.shutdown()

    watchdog_task: asyncio.Task | None = None
    idle_task: asyncio.Task | None = None

    async def watchdog():
        await asyncio.sleep(MAX_CALL_SECONDS)
        logging.warning("Max call length reached, hanging up")
        await hang_up("max_duration")

    async def idle_watchdog():
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - last_activity["t"] > IDLE_TIMEOUT_SECONDS:
                logging.warning("No speech for %ss, hanging up", IDLE_TIMEOUT_SECONDS)
                await hang_up("idle_timeout")
                return

    def append_call_log():
        total = time.monotonic() - start_time
        answered = end.get("answered_at_seconds")
        row = {
            "run_id": run_id,
            "started_at_utc": meta["started_at"],
            "scenario_id": scenario_id,
            "scenario_name": scenario["name"],
            "answered_at_s": answered if answered is not None else "",
            "call_duration_s": round(total - answered, 1) if answered is not None else 0,
            "end_reason": end.get("reason", "unknown"),
            "turns": stats["turns"],
            "endpointing_min_s": TURN_SETTINGS["endpointing"]["min_delay"],
            "endpointing_max_s": TURN_SETTINGS["endpointing"]["max_delay"],
            "folder": str(output_dir),
            "telnyx_cost_usd": "",  # fill in from the Telnyx usage report
            "notes": "",            # what you heard / what you changed after this call
        }
        is_new = not CALL_LOG.exists()
        with CALL_LOG.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row))
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    async def on_shutdown():
        if watchdog_task:
            watchdog_task.cancel()
        if idle_task:
            idle_task.cancel()
        end.setdefault("reason", "unknown")
        end["ended_at"] = datetime.now(timezone.utc).isoformat()
        try:
            write_meta()
            append_call_log()
        except Exception:
            logging.exception("Failed to write final call metadata")

    ctx.add_shutdown_callback(on_shutdown)

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        if participant.identity == SIP_IDENTITY:
            end.setdefault("reason", "receptionist_hung_up")
            ctx.shutdown()

    await ctx.connect()

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4o"),
        tts=deepgram.TTS(model="aura-2-thalia-en"),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(**TURN_SETTINGS),
    )

    @session.on("conversation_item_added")
    def on_conversation_item(event):
        item = event.item

        if getattr(item, "type", None) != "message":
            return

        role = getattr(item, "role", None)

        if role == "user":
            speaker = "RECEPTIONIST"
        elif role == "assistant":
            speaker = "PATIENT"
        else:
            return

        content = getattr(item, "text_content", None)
        if not content:
            return

        save_turn(speaker, content)

    # --- start listening FIRST, then dial, so the receptionist's greeting isn't missed.
    # If the session fails to start, say so loudly and tear the call down.
    session_task = asyncio.create_task(
        session.start(
            agent=PatientAgent(instructions, hang_up, transfer_pending),
            room=ctx.room,
            record=True,
        )
    )

    def on_session_started(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logging.error("Agent session failed to start", exc_info=exc)
            spawn(hang_up("session_failed"))

    session_task.add_done_callback(on_session_started)

    # ringing_timeout and max_call_duration are enforced by LiveKit's SIP service too, as a
    # backstop in case this process dies. The asyncio timeout below covers a stuck request.
    sip_request = api.CreateSIPParticipantRequest(
        sip_trunk_id=trunk_id,
        sip_call_to=TARGET_NUMBER,
        sip_number=caller_number,
        room_name=ctx.room.name,
        participant_identity=SIP_IDENTITY,
        participant_name="Receptionist",
        wait_until_answered=True,
        ringing_timeout=Duration(seconds=DIAL_TIMEOUT_SECONDS),
        max_call_duration=Duration(seconds=MAX_CALL_SECONDS + 30),
    )

    try:
        logging.info("Dialing the assessment line (ring limit %ss)", DIAL_TIMEOUT_SECONDS)
        await asyncio.wait_for(
            ctx.api.sip.create_sip_participant(sip_request),
            timeout=DIAL_TIMEOUT_SECONDS + 15,
        )
    except Exception:
        logging.exception("Outbound call failed or was not answered")
        await hang_up("dial_failed")
        return

    end["answered_at_seconds"] = round(time.monotonic() - start_time, 1)
    logging.info("Call answered after %.1fs", end["answered_at_seconds"])

    # Both clocks start at ANSWER, independent of the session task.
    last_activity["t"] = time.monotonic()
    watchdog_task = asyncio.create_task(watchdog())
    idle_task = asyncio.create_task(idle_watchdog())

    try:
        await session_task
    except Exception:
        return  # already logged and cleaned up by on_session_started


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="patientprobe",
        )
    )