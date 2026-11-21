# 04 — Full fixed Tool list with schema conversion

Status: ready-for-agent
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

- [ ] A unit test checks every produced schema against the Codie's limits.
- [ ] Turn on, turn off and climate-set-temperature work against the fake Codie on exposed test entities. A call targeting a non-exposed entity fails, and nothing in Home Assistant changes.
- [ ] Removing an Assist tool from the test setup removes the matching Codie Tool, and the catalog never exceeds 16 Tools.
- [ ] Manual: "turn on my bedroom TV" and "set the AC to 24" work on the real Codie.

## Comments
