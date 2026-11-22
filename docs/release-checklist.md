# Release checklist

Run this against a real Codie before each release. The automated tests cover the protocol against a fake Codie, and these checks cover what only the real one can show. Copy the list into the release's issue and tick each item, or link the issue filed for it.

## Before you start

- [ ] The Codie is on software 1.6.0, awake, and a user is signed in.
- [ ] Home Assistant runs the release candidate, installed from this repo (HACS or a copy of `custom_components/codie`).
- [ ] Debug logging is on for `custom_components.codie` (see the README).
- [ ] At least these entities are exposed to Assist: one you can switch on and off (a light or a TV), and a climate entity (an AC or thermostat). One switchable entity is left unexposed.
- [ ] Note the versions: integration, Home Assistant, Codie software, and the Codie's AI language.

## Checks

Talk to the Character by voice unless a step says otherwise. Start a new conversation for each spoken check.

1. **Pairing**
   - [ ] Add the integration with the Codie's host and port 8081. The Codie shows a code. Entering it creates an entry titled `Codie (<host>)`.
   - [ ] The Codie's device has a **Connected** sensor that turns on within a few seconds, and an **Announce** entity.
   - [ ] With the integration disabled, a new conversation has no home-control Tools: asking to turn on an Exposed entity does nothing. Enable it again.
   - [ ] Start pairing again, then cancel the form while the code is showing. The Codie stops showing the code.
2. **Turn an Exposed entity on and off.** Say "turn on <exposed entity>", then "turn off <exposed entity>".
   - [ ] The entity changes state each time, and the Character confirms in its own words.
3. **Set the AC.** Say "set the AC to 24".
   - [ ] The climate entity's target temperature becomes 24 and the Character confirms.
4. **An entity that is not exposed.** Ask to turn on the unexposed entity, by its name.
   - [ ] Its state does not change. The Character says it can't find or control it. It must not act on another entity instead.
5. **Home Assistant restart.** Restart Home Assistant and wait for **Connected** to turn on. Then start a new conversation and repeat check 2.
   - [ ] The Character still turns the entity on and off.
6. **Announcement.** Call `notify.send_message` on the Announce entity with a sentence, while the Character is idle, and again while it is talking.
   - [ ] The Character speaks the text word for word, and cuts itself off for it the second time.
   - [ ] The text does not appear in the Character's chat history.
7. **Queued announcement.** Start a conversation, and while the Character is talking, call `codie.announce` with `queue: true` and a message under 200 characters.
   - [ ] The Character finishes, then speaks the message word for word.
   - [ ] The message appears in its chat history as something it said.
   - [ ] A message over 200 characters fails at once with a validation error, and nothing is spoken.
8. **Announcement while the Codie is asleep.** Let the Codie go to sleep or lock its screen, then call `notify.send_message`.
   - [ ] The action fails with an error that names the reason (for example `DEVICE_BLOCKED`), the screen stays off, and nothing is spoken.

## When a check fails

File a new issue for each failure. Include:

- for an HTTP request (Pairing, registering the Tools, an Announcement), the method, path, HTTP status, error code and message. Debug logging shows them as `GET /v1/api/... returned 409 DEVICE_BLOCKED: ...`, already redacted,
- for a Tool call, the `turn_id`: a failed call logs it as a warning, and debug logging shows it for every call,
- for an Announcement, the `request_id` from the debug log. A Queued announcement has none, so give the time it was sent,
- the versions noted above.

Never include the stored credentials (`local_secret`, `local_session_id`) or a request's `X-Local-*` signing headers, in the issue or in any log you share.

## Publishing the release

Releases are cut by [release-please](https://github.com/googleapis/release-please) from the Conventional Commit messages on `main`. Don't tag or edit the version by hand.

1. Each push to `main` updates an open pull request titled `chore(main): release X.Y.Z`. It bumps `version` in `custom_components/codie/manifest.json` and adds the release's entries to `CHANGELOG.md`. The version follows the commits: `fix:` bumps the patch, `feat:` the minor, and a breaking change (`!`) the minor while the version is below 1.0.0.
2. Run the checks above against that version. To ship a different version, push an empty commit to `main` with a `Release-As: X.Y.Z` footer.
3. Merge the release pull request. The workflow tags the merge commit `vX.Y.Z` and publishes a GitHub release with the changelog, which HACS offers as the update.
