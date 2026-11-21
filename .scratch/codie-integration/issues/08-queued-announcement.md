# 08 — Queued announcement

Status: ready-for-agent
Type: task
Blocked by: 07

Spec: [spec.md § Announcements](../spec.md#announcements)

## What to build

`codie.announce` with `queue: true` sends a Queued announcement with `POST /data/user/agent/results`. The Codie speaks it once the Character is idle and saves it to chat history.

- 1–200 characters after trimming. Anything longer is a validation error, never truncated.
- `503` means the speech service isn't ready and becomes a Home Assistant error.
- The `notify` entity is not affected and still sends immediate Announcements.

## Acceptance criteria

- [ ] Tests cover success, the 200-character limit, and `503`.
- [ ] Manual: send a queued message while the Character is talking. It plays afterwards and appears in the Codie's chat history.

## Comments
