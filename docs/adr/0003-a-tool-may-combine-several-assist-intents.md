# A Tool may combine several Assist intents behind one action argument

A Tool may take an `action` enum and map each value to a different Assist intent, for example `media_control` with `pause`, `resume`, `next` and `previous` in place of four Tools. This partly supersedes ADR-0002, where every Tool was one renamed Assist tool. We did it because the 16-Tool cap was full, and Tools that differ only in which intent they run waste places that the Character needs for new abilities. We still route every action through an Assist intent, so name, area and alias resolution and the Expose list behave exactly as in ADR-0002. The list stays fixed.

We only combine intents that take the same arguments. An argument that applies to some actions and not others, such as a volume level, is easy for the model to get wrong, so `set_volume` and `media_search_play` stay separate Tools.

## Consequences

- A combined Tool's schema and description are ours, not Assist's, so we maintain them when Assist's intents change.
- Exposed scripts still get no Tool of their own. `turn_on` runs an exposed script, but cannot pass the script's fields.
