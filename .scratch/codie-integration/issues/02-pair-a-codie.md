# 02 — Pair a Codie and see it as a device

Status: ready-for-agent
Type: task
Blocked by: none

Spec: [spec.md § Setup and Pairing](../spec.md#setup-and-pairing)

## What to build

The thinnest end-to-end slice: a user adds the "CODE27 Codie" integration, enters the host and port, types the code the Codie shows, and ends up with a device in Home Assistant.

- Scaffold `custom_components/codie` (manifest, `hacs.json`, config flow, translations) to core quality standards.
- A signed API client: canonical string `METHOD|PATH|TIMESTAMP|NONCE|PAYLOAD_MODE|BODY_HASH|USER_ID`, all `X-Local-*` headers, a new nonce on every request, the body hash computed over the exact bytes sent, and the session-id header when it isn't empty.
- Config flow: ping → bootstrap (abort if the Codie is unreachable or no user is signed in) → `pair/request` → a code form → `pair/confirm` with `auth_scope: "permanent"`. Call `pair/cancel` if the user aborts or the code expires.
- Store the credentials, `user_id`, and a newly generated stable lowercase UUID `integration_id` in the entry. Set the unique ID from the system endpoint (use the field found in issue 01, or a placeholder until then).
- Test harness: a fake Codie (aiohttp test server) that checks every signature by recomputing it. Later issues build on this harness.

## Acceptance criteria

- [x] The happy path creates one entry and one device per Codie. Adding the same Codie again aborts with "already configured".
- [x] A wrong code shows a form error, and the user can try again until the challenge expires.
- [x] The fake Codie rejects requests with a bad signature, a reused nonce or an old timestamp, and tests prove the client gets each right.
- [x] Session-scope credentials are never requested.

## Comments

Implemented 2026-11-21 (not committed: the directory is not a git repository).

- Client `custom_components/codie/api.py`, flow `config_flow.py`, fake Codie `tests/fake_codie.py`. 39 tests, mypy strict and ruff clean. The config flow is at 100% coverage.
- Deviation from the spec: an unreachable Codie shows a `cannot_connect` form error, so the user can fix a mistyped host. It does not abort. No signed-in user (also `DEVICE_NOT_LOGGED_IN`, or an empty `user_id`) does abort, with `not_signed_in`.
- The duplicate check runs after `pair/confirm`, because `machine_id` needs a signed call. Re-adding a configured Codie therefore still asks for a code and leaves one unused permanent secret on the Codie. If `pair/confirm`'s `device_id` is confirmed to equal `machine_id`, the check could move earlier.
- The manifest's GitHub URLs and the `@jaycdev` codeowner are guesses from the API repo's owner. Fix them when the real repository exists.
- No `quality_scale.yaml` or README yet (issue 09).
- Running the tests on Windows needs two venv-only stubs (`fcntl.py`, `resource.py` in site-packages) plus the socket patch in `tests/conftest.py`. Linux needs none of them.
