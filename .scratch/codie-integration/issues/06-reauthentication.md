# 06 — Re-authentication when Pairing stops working

Status: ready-for-agent
Type: task
Blocked by: 02

Spec: [spec.md § Re-authentication](../spec.md#re-authentication)

## What to build

- `401 LOCAL_USER_MISMATCH`, `LOCAL_SECRET_MISMATCH`, `INVALID_SIGNATURE` and `LOCAL_SESSION_EXPIRED` raise `ConfigEntryAuthFailed`, which starts the reauth flow.
- The reauth flow re-runs Pairing (ping → bootstrap → request → code → confirm) and updates the existing entry in place. It keeps the same `integration_id`, so the Codie's registration is reused.
- `401 REQUEST_EXPIRED` logs a clock-skew warning and does not start reauth.
- Switching Character Slots must not trigger reauth.

## Acceptance criteria

- [ ] Each listed code starts reauth in tests. `REQUEST_EXPIRED` doesn't.
- [ ] A completed reauth reloads the entry with the new credentials and Tools come back online.
- [ ] Manual: sign a different user in on the Codie and check that Home Assistant shows the re-authenticate card.

## Comments
