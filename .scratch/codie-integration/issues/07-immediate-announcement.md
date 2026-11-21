# 07 — Immediate Announcement (`notify` entity and `codie.announce`)

Status: ready-for-agent
Type: task
Blocked by: 02

Spec: [spec.md § Announcements](../spec.md#announcements)

## What to build

Automations can make the Character speak.

- A `notify` entity "Announce" per Codie, working with `notify.send_message`.
- An action `codie.announce` (target: a Codie device; `message`; `queue` declared now, but only `false` is accepted until issue 08).
- Both read the current Slot (`GET /data/user/slot`) and `POST /data/user/tts/utterances` with a new `request_id`. On `409 SLOT_MISMATCH`, re-read the Slot once and retry.
- `409 DEVICE_BLOCKED` / `AMBIENT_MODE_BLOCKED` raise a Home Assistant error that names the reason. The screen is never woken.
- Return once the request is accepted (`202`), without waiting for playback. Text length is 1–16000 characters after trimming.

## Acceptance criteria

- [ ] Tests cover success, a Slot mismatch followed by a successful retry, a blocked Codie, and empty or oversized text.
- [ ] Manual: an automation makes the real Character say "The washing is done". When the Codie is asleep, the action fails with a clear error.

## Comments
