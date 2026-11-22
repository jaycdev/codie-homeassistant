# 09 — README, HACS packaging and manual release checklist

Status: resolved
Type: task
Blocked by: 04, 05, 06, 08

Spec: [spec.md § Testing](../spec.md#testing)

## What to build

- A README covering installation through HACS, Pairing, what the Character can do (the Tool list), how exposure controls access, and Announcements versus Queued announcements.
- A clear warning: the Expose list is the only safety control, so don't expose locks, alarms or garage doors unless you accept the Character operating them unprompted (ADR-0002).
- `docs/release-checklist.md` with the manual steps from the spec's Testing section, to run on the real Codie before each release.
- HACS validation and hassfest passing in CI, if the repo gets CI.

## Acceptance criteria

- [x] The README uses the glossary's terms (Codie, Character, Exposed entity, Tool, Announcement).
- [x] The checklist covers every manual check in the spec.

## Comments

Implemented 2026-11-22.

- `README.md` and `docs/release-checklist.md`. `tests/test_docs.py` fails if the README's Tool table and `TOOLS` differ.
- The checklist covers the spec's eight manual checks, plus cancelling Pairing, the 200-character limit, chat history, and checking that a disabled integration gives new conversations no Tools.
- For issue 10's failure reports, debug logging now shows each error response's method, path, status, code and message (never the body as a whole or the headers), each Tool call's `turn_id` (also in the warning for a failed call), and each Announcement's `request_id`.
- No CI in the repo, so HACS validation and hassfest were not added.
