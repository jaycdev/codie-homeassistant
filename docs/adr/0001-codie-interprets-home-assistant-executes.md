# The Codie interprets commands; Home Assistant only executes them

Voice commands are heard and understood by the Character's own model on the Codie, which calls Tools that this integration registers through the Codie's external tool integration API; Home Assistant never receives the user's words. We chose this over making the Codie a microphone/speaker for HA Assist (the Codie API offers no wake-word audio stream to an outside system, and the Character would lose its personality, memory and conversational context) and over a single "pass the sentence to Assist" Tool (HA's default agent is rigid about phrasing, and the Character's model is better at turning "set ac to 24c" into structured arguments).

## Consequences

- The Codie's limits on external tools shape the design: at most 16 Tools per integration, names matching `^[a-z][a-z0-9_]{0,23}$`, restricted JSON Schema parameters, a 20 s timeout, and no replay of calls lost when the connection drops.
- Home Assistant must keep a live WebSocket to each Codie; while it is down, new conversations get no home-control Tools.
