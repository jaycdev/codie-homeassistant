# Character-interpreted announcements

Research notes, 2026-11-26. Source: `jaycdev/codie-http-api` at commit
`bb84f679a111e156a5527a62f226c66826b4b3bb` (Codie 1.6.0 API package, device source commit
`231610b06faa184a8b3995c21d635dba32c149b8` per `manifest.json`). The repository contains only
docs, OpenAPI and examples, not the device source, so everything below is the documented
contract and has not been checked against device behaviour.

Permalink base: `https://github.com/jaycdev/codie-http-api/blob/bb84f679a111e156a5527a62f226c66826b4b3bb/`
(append the `path#Lnn` cited below).

## Question

Can Home Assistant hand the active Character some text (for example "the washing machine is
done") and have the Character's own model interpret it and say something in character, instead
of speaking the text as-is the way an Announcement or Queued announcement does today?

## Short answer

**Yes, through `POST /data/user/chat/turns`.** This is the documented "unified conversation API".
It takes a text input as a user turn, runs the Character's model on it (with the Character's
existing chat history and memory), and with `output.speech=device` speaks the reply on the Codie
in the Character's voice. Two options make it fit for Announcements:

- `tools: "none"` stops the model calling Tools during that turn, so the text cannot set off a
  home action.
- `retention: "none"` (or `"chat"`) controls what the turn leaves behind.

What the API does **not** have is a system, developer or "event" role. The text always goes in as
if the **user** said it. There is no flag telling the model "this comes from Home Assistant,
rephrase it". The framing has to go in the text itself, and how well the Character follows it
needs testing on a device.

`POST /data/user/agent/results` and Tool results do not give a separate route. `agent/results`
is spoken as written, and a Tool result only reaches the model when the model itself called the
Tool.

## Endpoints found

### 1. `POST /data/user/chat/turns`: conversation turn (the route)

Sources: `GUIDE.en.md#L119-L167`, `openapi.json#L14214-L14773` (request schema
`#L14241-L14388`, errors `#L14718-L14771`), reference client `examples/local_api.py#L240-L253`.

Request (JSON):

```json
{
  "request_id": "<new UUID>",
  "slot": 1,
  "input": {"type": "text", "text": "..."},
  "output": {"speech": "device"},
  "interrupt": false,
  "retention": "none",
  "tools": "none"
}
```

| Field | What the source says | Citation |
| --- | --- | --- |
| `request_id` | Required UUID idempotency key. It shares a namespace and a 256-entry cache with TTS utterances. | `openapi.json#L14350-L14354`, `GUIDE.en.md#L165-L167`, `#L187` |
| `slot` | Required, 1 to 5, must be the current Slot, otherwise `409 SLOT_MISMATCH`. | `openapi.json#L14365-L14369`, `GUIDE.en.md#L128`, `#L233` |
| `input` | `{"type":"text","text":...}`. Text is 1 to 16000 characters and must be nonempty after trimming. No role field. `additionalProperties: false`. | `openapi.json#L14247-L14267`, `GUIDE.en.md#L141` |
| `output.speech` | Required. `none`, `device` or `client`. With `device`, the turn reaches `completed` only after playback on the Codie has finished. `none` still runs the model but nothing is spoken. | `openapi.json#L14333-L14349`, `GUIDE.en.md#L57`, `#L163` |
| `interrupt` | Defaults to `true`, which ends whatever conversation, speech or task is running. `false` makes the turn fail with `DEVICE_BUSY` (`retryable=true`) and removes its user message from history. | `openapi.json#L14328-L14332`, `GUIDE.en.md#L148` |
| `retention` | `full` (default): saved to chat history, and conversation state, summary, long-term memory and diary are updated. `chat`: chat history only. `none`: nothing is saved and the reply only appears in `assistant_text`. **All modes still read the existing history and memory.** | `openapi.json#L14355-L14364`, `GUIDE.en.md#L150` |
| `tools` | `all` (default): device tools and external Tools are offered. `none`: no Tools at all for the turn. The guide suggests this for turns that "only need a reply and must not trigger device actions or external tasks". | `openapi.json#L14370-L14379`, `GUIDE.en.md#L152` |

Unknown fields are rejected (`GUIDE.en.md#L146`, `additionalProperties: false` at
`openapi.json#L14245`).

The response is `202` with a `ChatTurnSnapshot` (`openapi.json#L135-L317`): `turn_id`, `state`,
`assistant_text`, `user_chat_ids`, `assistant_chat_ids`, `error`, `version` and timestamps.
`state` is one of `queued`, `starting`, `receiving_input`, `thinking`, `responding`,
`cancelling`, `completed`, `failed` or `cancelled` (`openapi.json#L229-L242`). Poll with
`GET /data/user/chat/turns/{turn_id}` and cancel with `POST .../{turn_id}/cancel`, which takes
an empty body (`GUIDE.en.md#L121-L126`). Only the credentials that created a turn can see it
(`openapi.json#L14777`). Terminal turns are kept for 30 minutes. A restart, a Character or
user change, or clearing history can end turns or make them unreachable (`GUIDE.en.md#L167`).

Admission and errors: the `http` codes include `DEVICE_BLOCKED`, `AMBIENT_MODE_BLOCKED`,
`SLOT_MISMATCH`, `SERVICE_NOT_READY`, `TURN_LIMIT` and `TOKEN_QUOTA_EXCEEDED`. The task codes
include `DEVICE_BUSY`, `DEVICE_INTERRUPTED`, `TURN_TIMEOUT`, `TTS_EMPTY_OUTPUT`,
`CONVERSATION_FAILED` and `TOKEN_QUOTA_EXCEEDED` (`openapi.json#L14718-L14771`). The guide says to
check `/data/user/device/state` (`conversation_blocked`) before each request
(`GUIDE.en.md#L163`). The reference client does that at `examples/local_api.py#L244-L246`, and the
field is described at `openapi.json#L18599`. `x-capabilities` is empty and session credentials are
enough (`openapi.json#L14717`, `GUIDE.en.md#L59`). The integration pairs with `permanent`
(`custom_components/codie/api.py#L269`), so that works either way.

**Stated:** a text turn runs the model and the reply is spoken in the Character's voice. Tools can
be turned off for the turn, and what is saved is configurable.
**Implied:** the reply will be in character, because it is the normal conversation path with the
Character prompt, history and memory. The API does not say so explicitly.
**Unknown:** how the model takes user-role text that is really a Home Assistant event (see Open
questions).

### 2. `POST /data/user/tts/utterances`: Announcement (verbatim, for comparison)

"Reads text aloud directly in the current character's voice, without calling an LLM or writing to
chat history" (`openapi.json#L39441`). The guide says the same and gives Home Assistant action
results as the example use (`GUIDE.en.md#L169-L193`). Text is 1 to 16000 characters and there is
no queue. This is what `CodieApi.async_post_utterance` uses (`custom_components/codie/api.py#L446`).

### 3. `POST /data/user/agent/results`: Queued announcement (verbatim)

Body `{"text": ...}`, 1 to 200 characters. "Queued and spoken in the current character voice when
the engine is idle; it is saved to chat history as an assistant message before speech starts"
(`openapi.json#L988-L1127`, description at `#L990`, schema at `#L1015-L1031`). There is no field
for interpretation, a role or a follow-up. The text is stored as something the Character said,
so the model never processes it. It is used by `async_post_agent_result`
(`custom_components/codie/api.py#L454-L456`).

### 4. External Tools (`/data/user/tool-integrations/...` and the executor WebSocket)

The catalog PUT is at `openapi.json#L38876` and onward (description: Tools available to all
models, frozen into each new conversation session). The executor protocol is
`openapi.json#L47148-L47200`. A Tool result goes back to the model: "model result nests application
data under data.external_result" (`openapi.json#L47151`). The result statuses are `succeeded`,
`failed` and `needs_clarification` (`#L47194-L47198`). Every `invoke` carries `session_id`,
`turn_id` and `call_id` (`#L47152-L47165`), so a result only answers a call the model made in a
running conversation. At most 3 invocations can be pending per integration (`#L47167-L47174`).
The example executor shows the same request and response loop
(`examples/external_tools.py#L51-L60`).

**Stated:** Tool results are interpreted by the model, not spoken directly.
**Implied:** this is not a way to push an unprompted event, because there is no server-to-model
entry point without a model-initiated call. It could only be used if a chat turn first asks the
model to call a Tool such as "get pending home events". That is more complicated than putting the
event in the turn's text and needs no extra capability.

### 5. Persona material, for a Home Assistant side LLM

- `GET /data/user/character?slot=N` and `GET /data/user/character/list` return `name`,
  `description`, `character_prompt` ("Character prompt text; supplied values undergo prompt-budget
  and lock checks") and `prompt_locked` (`openapi.json#L9611-L9750`, `character_prompt` at
  `#L9670-L9673`, `prompt_locked` at `#L9744-L9747`, list at `#L10152` and onward). The integration
  already reads the list (`custom_components/codie/api.py#L309`).
- `GET /data/user/ai/system-prompt` returns `{system_prompt}` (`openapi.json#L7093-L7290`, schema
  at `#L7126-L7133`). The summary is "Get global system prompt", so it is device-wide rather than
  per Character.

**Unknown:** whether `character_prompt` is returned when `prompt_locked` is true, for example for
built-in or DLC Characters. It might come back empty or hidden. This approach would also not give
the voice of the Character's own model, its chat history or its memory.

### Not found

There is no endpoint to inject a system message or event, no field to make the model interpret
`agent/results` text, and no external trigger for proactive conversation.
`/data/user/proactive/config` only configures built-in triggers such as greetings, idle recovery,
Character or outfit changes and vision events (`openapi.json#L33961-L33963`).

## Candidate approaches, ranked

1. **Chat turn with framed text.** `POST /data/user/chat/turns` with `input.type=text`,
   `output.speech=device`, `tools=none`, `retention=none` (or `chat` if the Character should
   remember having said it), and `interrupt` chosen per call. The text frames the event, for
   example: *"(Home Assistant notice, not something I said: the washing machine has finished.
   Tell me in one short sentence, in your own words.)"* Poll the turn until it is terminal and
   report `assistant_text`. Pros: it is the Character's own model, persona, memory and voice, and
   the API is documented for exactly this shape. Cons: model latency and token use
   (`TOKEN_QUOTA_EXCEEDED` exists), the output is not deterministic, and it is user-role text.
   There is no idle queue either: `interrupt=false` fails with `DEVICE_BUSY` instead of waiting,
   so a Queued announcement-like variant would need Home Assistant to retry. This would be a
   third kind of announcement next to Announcement and Queued announcement. `CONTEXT.md` defines
   Announcement as "as-is", so it needs its own term there (for example "Interpreted
   announcement").
2. **Chat turn with `speech=none`, then an Announcement.** Run the turn with `output.speech=none`
   and `retention=none`, read `assistant_text`, check it (length, empty), then speak it with
   `tts/utterances` or `agent/results`. That last step can also give a queued, remembered version,
   within the 200-character limit. Pros: Home Assistant sees the text before anyone hears it and
   can fall back to the raw text if the model fails. Cons: two round trips and more latency.
3. **Home Assistant side LLM that imitates the Character.** Fetch `name`, `description` and
   `character_prompt` (plus the global `system_prompt`), rephrase with a Home Assistant
   conversation agent or AI task, then speak the result as an Announcement. Pros: no Codie model
   call and works when the Codie's model service is unavailable. Cons: not the Character's own
   model, memory or history. It depends on `character_prompt` being readable, and it sends the
   persona to another provider.
4. **A pull Tool.** Register a Tool such as "read pending home notices" and start a chat turn
   asking the Character to check it. This is only worth it if the Character should pull event
   details itself. Option 1 already covers this case.

## Open questions to verify on a device

1. Does a `tools=none`, `retention=none` text turn with `speech=device` speak an in-character
   rephrasing? Does the framing stop the Character replying *to* the user ("Oh, thanks for telling
   me!") instead of telling the user? Try several framings and Characters, including the
   built-in ones.
2. Is `user_chat_ids`/`assistant_chat_ids` really empty with `retention=none`, and does nothing
   appear in the Codie's chat history UI? With `retention=chat`, does the framed user message
   show up in the history the user sees?
3. Latency from POST to the start of speech, compared with `tts/utterances`.
4. Behaviour with `interrupt=false` while the Character is talking, during an Alarm, or in Music
   mode (`AMBIENT_MODE_BLOCKED`?), and with the screen off or the Codie locked
   (`DEVICE_BLOCKED`).
5. Does `tools=none` also stop built-in device actions such as setting an Alarm, as
   `GUIDE.en.md#L152` implies?
6. Is `character_prompt` returned for `prompt_locked` Characters, and what does
   `/ai/system-prompt` contain by default? Only relevant for approach 3.
7. Does a chat turn from Home Assistant interrupt a conversation the user is having (default
   `interrupt=true`), and does it count against the 256-entry turn cache shared with utterances?
