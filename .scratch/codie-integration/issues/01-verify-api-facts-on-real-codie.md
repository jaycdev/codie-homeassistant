# 01 — Verify open API facts on the real Codie

Status: resolved
Type: research
Blocked by: none

Spec: [spec.md § To verify during implementation](../spec.md#to-verify-during-implementation)

## What to do

Pair the user's Codie (1.6.0) using the official example script (`examples/local_api.py ... --auth-scope permanent`, with a new credentials filename). Then find out:

1. Which field of `GET /data/user/system` is the stable device identifier, the one to use as the config entry's unique ID.
2. Whether tool integrations are scoped per user or per Slot. Register the echo example from `examples/external_tools.py`, switch Slots, and check whether `GET /data/user/tool-integrations` still lists it and the new Character can still call it.
3. Whether the Codie advertises itself over mDNS or zeroconf. If it does, note the service type for a later discovery feature.
4. Whether `risk: consequential` or `confirmation_required: true` changes anything the Character does. This is only for the record.

## Acceptance criteria

- [x] Answers recorded under `## Answer` in this file.
- [x] The spec's "To verify" section is updated with the findings.
- [x] The credentials file is not committed or pasted anywhere.

## Answer

Run against the user's Codie (1.6.0) on 2026-11-21. Paired with `tools/pair_codie.py` (permanent scope; `local_session_id` came back empty). Credentials are in `~/.codie-ha/credentials.json`, outside the repo. The test integration was deleted afterwards and the Codie put back on its original Slot.

1. **Device identifier: `machine_id`** from `GET /data/user/system`. It is a 64-character lowercase hex string. Use it as the config entry's unique ID. The mDNS TXT record's `device_id` carries the same value, which backs this up and allows discovery to match it later. `user_id` (a UUID) identifies the signed-in user, not the device. `local_ip` is not stable. Not verified: whether `machine_id` survives a factory reset or software update.
   `GET /data/user/system` also returns `capabilities` (all `false` for this account), `language`, `ai_language`, `timezone`, `username`, `build_type` and `feature_tag`.
2. **Tool integrations are scoped per user, not per Slot.** A registered integration stayed listed (`online: true`, same revision) after switching from Slot 5 to Slot 1 (`PUT /data/user/slot`; loading finished in under 12 s), and the Slot 1 Character called the tool and answered with its result. So a Slot switch needs no re-registration. The spec's assumption holds.
   Other observations:
   - The executor WebSocket sends `{"type":"ready","data":{"integration_id","protocol_version","revision"}}` first.
   - An `invoke` message's `data` has: `arguments`, `call_id`, `deadline_unix_ms`, `integration_id`, `invocation_id`, `name` (the tool name), `revision`, `round_id`, `session_id`, `timeout_ms`, `turn_id` and `version`. `timeout_ms` was 4999 for a 5000 declaration.
   - The two invocations (Slot 5, then Slot 1) had different `session_id` and `turn_id` values. The Slot switch needed no executor reconnect.
   - The `PUT` response and `GET` list return the tools sorted by name, not in the order sent.
   - `DELETE /data/user/tool-integrations/{id}` replies with an empty body. The client must not assume JSON.
3. **The Codie advertises itself over mDNS** as service type `_codie-config._tcp.local`, port 8081. The instance name looks like `Codie-<base32 string>`. The TXT record has `device_id` (the `machine_id`), `name` (`Codie-<machine_id>-<ip>`), `version` (a build number, `261118175` on this Codie) and `ipv4`. A discovery feature can use `_codie-config._tcp.local` with the `device_id` TXT key. Home Assistant's zeroconf needs a `zeroconf` entry in `manifest.json` for that type.
   Note: the query only got an answer when sent from the LAN interface. A first attempt on a virtual adapter saw nothing.
4. **`confirmation_required: true` changes the Character's behaviour.** With `risk: consequential` and `confirmation_required: true` set together on one tool, the Character did not call it. It replied asking whether it may run the risky echo with the given text, and no `invoke` reached the executor. I didn't test the approval reply, or `risk` on its own, so the effect of each setting separately is unknown. The tool with `risk: read` and `confirmation_required: false` ran immediately. This fits the spec's choice of `confirmation_required: false` on every Tool.

## Comments
