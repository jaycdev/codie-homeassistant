# Tools are a fixed subset of Home Assistant's Assist tools

_Partly superseded by [ADR-0003](0003-a-tool-may-combine-several-assist-intents.md): a Tool may now combine several Assist intents._

The Tools offered to the Character are Home Assistant's own Assist LLM tools (turn on/off, set light, set temperature, live context, …), renamed to fit the Codie's naming rules and limited to a fixed list of at most 16. We reuse them because they already resolve spoken names, areas and aliases to Exposed entities; we fix the list because the full Assist set (one tool per exposed script, plus timers, lists, weather and more) exceeds the Codie's 16-Tool cap, and a list that grew or shrank with the user's setup would make the Character's abilities unpredictable.

## Consequences

- Exposed scripts are not offered as Tools, even though Assist offers them. Revisit in a later version. (Revisited in ADR-0003: `turn_on` runs them and lists their names.)
- The Expose list is the only safety control. Locks, alarms and garage doors are not singled out and nothing asks for confirmation, so the user should not expose anything the Character must not operate unprompted.
