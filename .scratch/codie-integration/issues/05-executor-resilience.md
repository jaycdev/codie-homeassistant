# 05 — Executor resilience and catalog refresh

Status: done
Type: task
Blocked by: 03

Spec: [spec.md § Executor connection](../spec.md#executor-connection)

## What to build

Keep Tools available over days of uptime.

- Reconnect with exponential backoff when the socket drops or the Codie restarts. Answer pings in time (pings every 20 s, 60 s pong timeout).
- Handle up to 3 invocations concurrently. Return `failed` before `deadline_unix_ms` when a Home Assistant call runs too long.
- Don't retry calls lost at disconnect. Log them with their `invocation_id` as having an unknown outcome.
- Re-register the catalog and reconnect when the set of available Assist tools changes, for example when an integration loads after startup or Home Assistant is upgraded.
- Handle the protocol's `error` messages by logging the code and reconnecting where that makes sense.

## Acceptance criteria

- [x] Tests: the fake Codie drops the socket and the client reconnects. A slow Home Assistant call yields a timely `failed`. Three concurrent invokes all get results.
- [x] Changing which Assist tools are available triggers a re-registration with the correct `expected_revision`.
- [x] The binary sensor goes off while the client is reconnecting and back on once `ready` returns.

## Comments
