# Interpreted announcements are a chat turn in the user's voice, with Tools off

An Interpreted announcement wraps the message in a fixed instruction ("Tell me this in your own words, as a short announcement. {instructions}. The message: {message}") and sends it to the active Character as a conversation turn (`POST /data/user/chat/turns`) with `tools=none`, spoken on the Codie (`speech=device`). The Codie's API has no system or event role, so the only way to reach the Character's own model, persona and memory is as text the user appears to have said. Turning Tools off is required, not a preference: without it, a notice such as "the lights are still on" could make the Character act on the home, which ADR-0001 reserves for the user's own requests.

The alternatives were a Home Assistant-side LLM imitating the Character from its `character_prompt`, which loses the Character's own model and memory and may be unreadable when the prompt is locked, and running the turn silently and speaking `assistant_text` through the Announcement path, which would allow checking and queuing but costs a second request and lets Home Assistant judge text it often cannot read (the reply is in the Character's language).

## Consequences

- With `remember`, the turn is saved to chat history (`retention=chat`), so the history shows the user saying the instruction, followed by the Character's reply. The API cannot save the reply alone.
- A reply takes about 8 seconds to start (measured on a Codie on 2026-11-26), which rules out time-critical uses such as a doorbell.
- Home Assistant cannot check the reply before it is spoken. It only sees `assistant_text` afterwards, in the `codie_announcement_finished` event.
- A chat turn cannot wait for the Character to be idle, so an Interpreted announcement always cuts in and cannot be queued.
- When the turn fails in the model (token quota, timeout, empty reply), the original text is spoken as a plain immediate Announcement, which is never remembered.
- Each Interpreted announcement spends the Codie's token quota.
