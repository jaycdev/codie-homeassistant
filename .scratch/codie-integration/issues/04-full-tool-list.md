# 04 — Full fixed Tool list with schema conversion

Status: resolved
Type: task
Blocked by: 03

Spec: [spec.md § Tools](../spec.md#tools), [ADR-0002](../../../docs/adr/0002-tools-are-a-fixed-subset-of-assist.md)

## What to build

Register the other 15 Tools from the spec's table. This is the slice that makes "turn on my bedroom TV" and "set AC to 24°C" work.

- A fixed mapping from Codie Tool name to Assist tool name, and nothing else. Exposed scripts and Assist tools outside the table are never offered.
- Convert each Assist tool's voluptuous parameter schema into the Codie's restricted JSON Schema: object root, no `$ref`, unions or patterns, depth ≤ 6, ≤ 64 properties, ≤ 8 KiB. A union collapses to its simplest usable branch.
- A small, stable `result_schema` per Tool, and a mapping from Assist's results and errors to `succeeded`, `failed` (with `code`) or `needs_clarification`.
- If an Assist tool is missing in the running Home Assistant version, leave its Tool out without a substitute.
- Use the risk values from the spec's table. Set `confirmation_required: false` everywhere and `timeout_ms: 10000`.

## Acceptance criteria

- [x] A unit test checks every produced schema against the Codie's limits.
- [x] Turn on, turn off and climate-set-temperature work against the fake Codie on exposed test entities. A call targeting a non-exposed entity fails, and nothing in Home Assistant changes.
- [x] Removing an Assist tool from the test setup removes the matching Codie Tool, and the catalog never exceeds 16 Tools.
- [x] Manual: "turn on my bedroom TV" and "set the AC to 24" work on the real Codie.

## Comments

- Manual check passed on Home Assistant Core 2026.11.2 (HAOS) with a real Codie (software 1.6.0): the Character read the home state and controlled the Study AC.
- Found while testing: HA 2026.11 moved `GetLiveContext` and the to-do query into integration `llm` platforms with prefixed names and `ToolResult` results; `voluptuous-openapi` has to be a manifest requirement; the Character guesses `device_class`, so it is hidden on turn_on/turn_off; a failed target match now lists the exposed names; the Codie refuses a changed Tool under an old version, so each Tool is versioned by a hash of its definition.
- Still open for issue 05: the catalog is built once at setup, so intents that register late are missed until it re-registers.
