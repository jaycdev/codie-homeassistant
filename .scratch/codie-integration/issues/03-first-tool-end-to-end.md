# 03 — First Tool end to end: `get_home_state`

Status: resolved
Type: task
Blocked by: 02

Spec: [spec.md § Tools](../spec.md#tools), [Executor connection](../spec.md#executor-connection), [Entities](../spec.md#entities-per-codie)

## What to build

The tracer slice for home control: the Character can ask Home Assistant what is going on in the home.

- On setup, register a catalog containing the single Tool `get_home_state` with `PUT /data/user/tool-integrations/{integration_id}`. Use `expected_revision` 0 on create, the current revision on update, and re-read and retry on `409`.
- Open the executor WebSocket, wait for `ready`, and answer each `invoke` by calling Assist's GetLiveContext through `llm.async_get_api(hass, LLM_API_ASSIST, ...)`. Send back `result` with a matching `invocation_id`.
- Add a "Connected" connectivity `binary_sensor` that is on while the executor is `ready`.
- Unloading closes the socket. Removing the entry deletes the tool integration on the Codie.

## Acceptance criteria

- [x] Against the fake Codie: registration, `ready`, an `invoke` producing a `succeeded` result whose data matches the registered `result_schema`.
- [x] The result contains only Exposed entities. A test exposes one entity and hides another.
- [x] The binary sensor follows the connection state.
- [x] Removing the entry calls `DELETE` on the tool integration.
- [x] Manual: ask the real Codie's Character "what lights are on?" and get a correct answer.

## Comments

Implemented 2026-11-21. Wire format taken from github.com/jaycdev/codie-http-api (openapi.json, examples/external_tools.py).

- New: `tools.py` (catalog, registration with 409 re-read/retry, Assist call), `executor.py`, `errors.py`, `binary_sensor.py`. The fake Codie gained tool-integration endpoints and a signed executor WebSocket. 49 tests, mypy strict and ruff clean.
- The executor WebSocket signature goes in query parameters, as `local_api.py` does. Confirmed on the real Codie (1.6.0).
- The deadline is `timeout_ms` minus 0.5 s from arrival, not `deadline_unix_ms`, so clock skew cannot break it.
- Not done: reconnect after a dropped socket (issue 05); skipping a Tool whose Assist tool is missing (issue 04); `ai_language` for the LLM context.
- Test setup: `hassil==3.5.0` and `home-assistant-intents==2026.1.28` are pinned in `requirements_test.txt` to match Home Assistant 2026.2.3. The manifest has `after_dependencies: ["conversation"]` so the Assist intents are loaded.

Manual check 2026-11-21, real Codie (1.6.0, `ai_language` `ja`): the repo's own client registered a throwaway integration, the executor connected with the query-signed handshake, and a text chat turn ("which lights are on?", retention `none`, no speech) made the Character call `get_home_state`. It answered correctly in Japanese from a stub result (kitchen lamp on, bedroom light off). The integration was deleted afterwards. Not tested: real Exposed entities, which need a running Home Assistant.
