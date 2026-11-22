# Spec: CODE27 Codie integration for Home Assistant (v1)

Status: ready-for-planning

Vocabulary follows [CONTEXT.md](../../CONTEXT.md). Decisions: [ADR-0001](../../docs/adr/0001-codie-interprets-home-assistant-executes.md), [ADR-0002](../../docs/adr/0002-tools-are-a-fixed-subset-of-assist.md).

## Goal

The user says to their Codie "Hey Codie, turn on my bedroom TV" or "Hey Codie, set the AC to 24°C", and the active Character makes it happen. The Character only sees and controls Exposed entities. Home Assistant automations can also make the Character speak.

## Shape

- Custom integration, domain `codie`, display name "CODE27 Codie", distributed through HACS and written to Home Assistant core's quality standards.
- One config entry per Codie. Several Codies per Home Assistant instance are supported.
- The Character's model on the Codie interprets the request and calls a Tool. Home Assistant runs the Tool against Exposed entities and returns the result, and the Character phrases the reply itself (ADR-0001).
- Target: Codie software 1.6.0 HTTP API (`http://HOST:8081/v1/api`), API package source commit `231610b`.

## Setup and Pairing

1. The user adds the integration and enters the host and port (default 8081). No discovery in v1.
2. The integration calls `GET /auth/ping`, then `GET /auth/bootstrap` (which returns `user_id` and signed-in state). It aborts with a clear error if the Codie is unreachable or no user is signed in.
3. `POST /auth/pair/request` makes the Codie display a code. The user types the code into the form. `POST /auth/pair/confirm` with `auth_scope: "permanent"` returns the credentials. Session-scope credentials are never used, because they expire after 12 h and on Codie restart.
4. The config entry stores `local_secret`, `local_session_id` (if not empty), `user_id`, and a newly generated, stable lowercase UUID used as the tool `integration_id`. The unique ID comes from `/data/user/system` (the device identifier); confirm which field during implementation.
5. If the user cancels or the code expires (5 min), call `POST /auth/pair/cancel`.
6. Removing the config entry calls `DELETE /data/user/tool-integrations/{integration_id}`.

Every request except the five pairing and bootstrap endpoints is HMAC-signed as described in the Codie guide §4: a new nonce on every request including retries, the timestamp in seconds, and the hash computed over the exact body bytes sent.

### Re-authentication

`401 LOCAL_USER_MISMATCH`, `LOCAL_SECRET_MISMATCH`, `INVALID_SIGNATURE` (after a clock check) and `LOCAL_SESSION_EXPIRED` start Home Assistant's standard re-authentication flow, which re-runs Pairing. A Slot switch does not, because Tools are registered per user, not per Slot. `REQUEST_EXPIRED` is a clock-skew problem and is logged rather than triggering re-auth.

## Tools

Registered with `PUT /data/user/tool-integrations/{integration_id}` (`protocol_version: v1`, `expected_revision` from the last `GET`, or 0 on create; on `409`, re-read and retry). They are served over the executor WebSocket `/v1/ws/tool-integrations/{integration_id}/executor`.

Each Tool wraps one of Home Assistant's Assist LLM API tools (`llm.LLM_API_ASSIST`), so Exposed-entity filtering and name, area and alias matching come from Home Assistant (ADR-0002). The list is fixed:

| Codie Tool | Assist tool | risk |
| --- | --- | --- |
| `get_home_state` | GetLiveContext | read |
| `turn_on` | HassTurnOn | reversible |
| `turn_off` | HassTurnOff | reversible |
| `light_set` | HassLightSet | reversible |
| `climate_set_temp` | HassClimateSetTemperature | reversible |
| `set_position` | HassSetPosition | reversible |
| `fan_set_speed` | HassFanSetSpeed | reversible |
| `media_pause` | HassMediaPause | reversible |
| `media_unpause` | HassMediaUnpause | reversible |
| `media_next` | HassMediaNext | reversible |
| `media_previous` | HassMediaPrevious | reversible |
| `set_volume` | HassSetVolume | reversible |
| `media_search_play` | HassMediaSearchAndPlay | reversible |
| `vacuum_start` | HassVacuumStart | reversible |
| `list_add_item` | HassListAddItem | reversible |
| `todo_get_items` | todo_get_items | read |

Rules:
- If an Assist tool is missing in the running Home Assistant version, leave its Tool out and don't substitute another. Re-register the catalog when the set changes, for example after a Home Assistant upgrade or an integration loading.
- `confirmation_required: false` on every Tool. The Expose list is the only safety control (ADR-0002). The README warns not to expose locks, alarms or garage doors unless the user accepts the Character operating them unprompted.
- Parameter schemas are converted from the Assist tool's voluptuous schema into the Codie's restricted JSON Schema: object root, no `$ref`, unions or patterns, depth ≤ 6, ≤ 64 properties, ≤ 8 KiB. A union (for example an `Any` of a string or a list) collapses to its simplest usable branch. Each Tool gets a `result_schema` with a stable, small shape (≤ 16 KiB of data).
- `timeout_ms` defaults to 10000 and must not exceed 20000. If the Home Assistant call misses the deadline, return `failed` before `deadline_unix_ms`.
- Descriptions are the Assist descriptions, kept short. `get_home_state`'s description tells the model to call it when it's unsure of a device name or state.
- Exposed scripts are not Tools in v1.

### Executor connection

- Connect after registration, and wait for `ready`. Answer each `invoke` with a `result` (`succeeded`, `failed` with a `code`, or `needs_clarification`), matching `invocation_id`.
- At most 3 pending calls per integration are allowed. Calls are handled concurrently.
- Reconnect with backoff if the connection drops. Calls in flight at disconnect have an unknown outcome and are not retried by either side.
- Directory or catalog changes need a reconnect. Tools are frozen into each new Character conversation, so a reconnect only affects later conversations.
- The connection made last replaces any earlier one. Two Home Assistant instances paired to the same Codie with the same `integration_id` would compete for it, which can't happen because each config entry generates its own ID.

## Announcements

- **`notify` entity per Codie.** `notify.send_message` sends an immediate Announcement through `POST /data/user/tts/utterances`. It cuts off whatever the Character is doing, isn't saved to chat history, and accepts 1–16000 characters.
- **Action `codie.announce`** (target: a Codie device; fields `message`, optional `queue` boolean):
  - `queue: false` (default): same as the `notify` entity.
  - `queue: true`: a Queued announcement through `POST /data/user/agent/results`. The Codie speaks it once the Character is idle and saves it to chat history. 1–200 characters; longer messages are rejected with a validation error.
- The immediate path reads the current Slot from `GET /data/user/slot` before sending. On `409 SLOT_MISMATCH`, re-read the Slot once and retry. The queued path sends only `{"text": ...}` and has no Slot, so it neither reads nor retries.
- The screen is never woken automatically. If the Codie is asleep or locked (`409 DEVICE_BLOCKED` / `AMBIENT_MODE_BLOCKED`), the action fails with an error that names the reason.
- The action returns once the request is accepted (`202`). Waiting for playback to finish is not in v1.

## Language

The language the Character speaks and understands is the Codie's AI language, read from the `ai_language` key of `GET /data/user/system` (for example `ja`). It is not the same as the `language` key, which is the device UI language. Both Tools and Announcements run in that language: the Character phrases its replies in it (ADR-0001) and speaks Announcement text as given. The integration only reads `ai_language`. It never changes it, and it doesn't translate Announcement text.

## Entities (per Codie)

- `binary_sensor` "Connected" (connectivity): on while the executor WebSocket is `ready`.
- `notify` "Announce": see above.

## Out of scope for v1

Exposed scripts as Tools, discovery, screen, brightness, music, lighting, memory and diary entities, confirmation for sensitive domains, using the Codie as an Assist satellite or conversation agent, and waiting for an Announcement's playback to complete.

## Testing

- Automated: `pytest-homeassistant-custom-component` against a fake Codie (aiohttp test server). It covers HMAC signing, which is checked by recomputing the signature from the canonical string, along with: Pairing and its cancellation, re-authentication triggers, catalog registration with revision conflicts, the executor WebSocket (invoke/result, timeout, disconnect), schema conversion staying inside the Codie's limits, both Announcement paths, and Slot mismatch.
- Manual: a short checklist run against the user's own Codie on 1.6.0 before each release. It covers pairing, "turn on/off <exposed entity>", "set AC to 24", a request about a non-exposed entity (it must not act), a Home Assistant restart followed by a new conversation, an immediate Announcement, a Queued announcement, and an Announcement while the Codie is asleep (it must fail clearly).

## To verify during implementation

- Which Assist tool names exist in the target Home Assistant version and what parameters they take.
- ~~Which `/data/user/system` field is the stable device identifier.~~ Found: `machine_id` (64-char hex), also the `device_id` in the mDNS TXT record. See issue 01. Still to confirm that it survives a reset or software update.
- ~~Whether tool integrations are scoped per user (assumed) or per Slot.~~ Found: per user. A Slot switch keeps the registration and the new Character can call the tools. See issue 01.
- ~~The real behaviour of `confirmation_required` and `risk` on the Codie.~~ Recorded in issue 01: a tool with `risk: consequential` and `confirmation_required: true` made the Character ask for approval instead of calling it. The effect of each setting alone wasn't tested.
- For later (discovery, out of scope for v1): the Codie advertises `_codie-config._tcp.local` on port 8081, with TXT keys `device_id`, `name`, `version` and `ipv4`.
