"""Place one test call: dispatches the patientprobe worker into a new room.

Start the worker first, in another terminal:
  python agent.py dev

Then place calls:
  python make_call.py 07
  python make_call.py 02 --booked-slot "Tuesday, November 3 at 2:30 PM"
  python make_call.py 03 --current-slot "Thursday, November 5 at 3:00 PM"

Scenarios 02 and 03 act on a REAL appointment from an earlier call. Read that call's
transcript, confirm the receptionist actually confirmed the slot, and pass it exactly.
03 must use the slot that is still booked after 02 (if 02 failed, that's 01's slot).

The number dialed is fixed inside agent.py; this script only picks the scenario.
"""
import argparse
import asyncio
import json
import sys
import uuid

from dotenv import load_dotenv
from livekit import api

from scenarios import (
    REQUIRED_OVERRIDES,
    build_instructions,
    get_scenario,
    require_caller_number,
)

load_dotenv()

FLAGS = {"booked_slot": "--booked-slot", "current_slot": "--current-slot"}


def confirm_slots(overrides: dict) -> None:
    print("This scenario acts on a REAL appointment from an earlier call:")
    for key, value in overrides.items():
        print(f"  {key}: {value}")
    answer = input("Did that call's transcript clearly confirm this slot? [y/N] ")
    if answer.strip().lower() != "y":
        sys.exit("Aborted. Check the earlier transcript first.")


async def main(scenario_id: str, overrides: dict):
    scenario = get_scenario(scenario_id)  # fail fast on a bad id, before spending anything
    room_name = f"patientprobe-{scenario_id}-{uuid.uuid4().hex[:6]}"

    lk = api.LiveKitAPI()
    try:
        print(f"Scenario {scenario_id}: {scenario['name']}")
        print(f"Creating room: {room_name}")
        await lk.room.create_room(api.CreateRoomRequest(name=room_name))

        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name="patientprobe",
                metadata=json.dumps(
                    {"scenario_id": scenario_id, "overrides": overrides}
                ),
            )
        )
        print("Dispatched. Watch the worker terminal for dialing logs.")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario_id", help="e.g. 01, 07, 16")
    parser.add_argument("--booked-slot", help="REQUIRED for 02: the slot confirmed in call 01")
    parser.add_argument("--current-slot", help="REQUIRED for 03: the slot that is still booked")
    parser.add_argument("--yes", action="store_true", help="skip the slot confirmation prompt")
    args = parser.parse_args()

    try:
        require_caller_number()
        get_scenario(args.scenario_id)
    except ValueError as e:
        parser.error(str(e))

    overrides = {}
    if args.booked_slot:
        overrides["booked_slot"] = args.booked_slot
    if args.current_slot:
        overrides["current_slot"] = args.current_slot

    required = REQUIRED_OVERRIDES.get(args.scenario_id, ())
    missing = [FLAGS[k] for k in required if k not in overrides]
    if missing:
        parser.error(
            f"scenario {args.scenario_id} needs {', '.join(missing)} "
            "(a slot confirmed in an earlier call's transcript)"
        )

    unexpected = [FLAGS[k] for k in overrides if k not in required]
    if unexpected:
        parser.error(
            f"scenario {args.scenario_id} does not accept {', '.join(unexpected)}"
        )

    # Build the real prompt now, so a problem shows up here and not in the worker log.
    try:
        build_instructions(args.scenario_id, **overrides)
    except (ValueError, KeyError) as e:
        parser.error(f"Cannot build scenario: {e}")

    if required and not args.yes:
        confirm_slots(overrides)

    asyncio.run(main(args.scenario_id, overrides))