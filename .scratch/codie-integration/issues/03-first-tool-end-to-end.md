# 03 — First Tool end to end: `get_home_state`

Status: ready-for-agent
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

- [ ] Against the fake Codie: registration, `ready`, an `invoke` producing a `succeeded` result whose data matches the registered `result_schema`.
- [ ] The result contains only Exposed entities. A test exposes one entity and hides another.
- [ ] The binary sensor follows the connection state.
- [ ] Removing the entry calls `DELETE` on the tool integration.
- [ ] Manual: ask the real Codie's Character "what lights are on?" and get a correct answer.

## Comments
