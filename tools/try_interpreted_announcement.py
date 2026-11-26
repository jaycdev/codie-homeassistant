#!/usr/bin/env python3
"""Throwaway: does the Character rephrase a message in its own personality?

Usage:
    python tools/try_interpreted_announcement.py http://DEVICE_IP:8081
    python tools/try_interpreted_announcement.py http://DEVICE_IP:8081 --message "The dryer is done"
    python tools/try_interpreted_announcement.py http://DEVICE_IP:8081 --speech none

For each framing, sends the message as a chat turn (POST /data/user/chat/turns) with no Tools
and nothing saved, waits for the turn to end and prints the Character's reply. With the default
--speech device the Codie speaks each reply; with none it only prints them.

Reuses the client and the credentials file from tools/pair_codie.py (pair with that first).
Not part of the integration; see docs/research/character-interpreted-announcements.md.
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from pair_codie import API, DEFAULT_CREDENTIALS, APIError, CodieClient

FRAMINGS = {
    "bare": "{message}",
    "own-words": "Tell me this in your own words, as a short announcement: {message}",
    "relay": (
        "This is a notice from my smart home, not something I'm telling you. "
        "Announce it to me in one short sentence, in your own style: {message}"
    ),
}
TERMINAL_STATES = ("completed", "failed", "cancelled")


def run_turn(client, text, speech, retention, interrupt, timeout):
    """Send one text turn and return its final snapshot and how long it took."""
    if client.request("GET", API + "/data/user/device/state")["data"].get("conversation_blocked"):
        raise RuntimeError("The Codie is blocking conversations right now (asleep, locked or busy).")
    slot = client.request("GET", API + "/data/user/slot")["data"]["slot_number"]
    started = time.monotonic()
    turn = client.request("POST", API + "/data/user/chat/turns", {
        "request_id": str(uuid.uuid4()),
        "slot": slot,
        "input": {"type": "text", "text": text},
        "output": {"speech": speech},
        "interrupt": interrupt,
        "retention": retention,
        "tools": "none",
    })["data"]
    path = API + "/data/user/chat/turns/" + turn["turn_id"]
    while turn.get("state") not in TERMINAL_STATES:
        if time.monotonic() - started > timeout:
            client.request("POST", path + "/cancel")
            raise RuntimeError(f"The turn did not end within {timeout} s; cancelled it.")
        time.sleep(1)
        turn = client.request("GET", path)["data"]
    return turn, time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description="Try Character-interpreted announcements.")
    parser.add_argument("device", help="http://DEVICE_IP:8081 (no /v1/api)")
    parser.add_argument("--message", default="Washing machine is finished")
    parser.add_argument("--framing", choices=FRAMINGS, action="append",
                        help="framing to try; repeat for several (default: all)")
    parser.add_argument("--speech", choices=("device", "none"), default="device")
    parser.add_argument("--retention", choices=("none", "chat"), default="none")
    parser.add_argument("--interrupt", action="store_true",
                        help="cut off whatever the Codie is doing (default: fail if it is busy)")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    args = parser.parse_args()

    if not args.credentials.exists():
        raise RuntimeError(f"No credentials at {args.credentials}; run tools/pair_codie.py first.")
    client = CodieClient(args.device, json.loads(args.credentials.read_text(encoding="utf-8")))

    for name in args.framing or FRAMINGS:
        text = FRAMINGS[name].format(message=args.message)
        print(f"\n== {name} ==\nsent:  {text}")
        try:
            turn, seconds = run_turn(
                client, text, args.speech, args.retention, args.interrupt, args.timeout
            )
        except APIError as error:
            print(f"error: {error}")
            continue
        print(f"reply: {turn.get('assistant_text') or '(none)'}")
        print(f"state: {turn.get('state')}, {seconds:.1f} s")
        if turn.get("error"):
            print(f"error: {json.dumps(turn['error'], ensure_ascii=False)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, APIError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
