# Timers run a checked Command through Home Assistant's built-in agent

A Timer without a Command is a plain countdown that only rings (see Consequences). Otherwise a Timer carries a Command, a short sentence the Character's model writes ("turn off the bedroom lights"). When the Timer ends, the integration runs it through Home Assistant's built-in conversation agent (`conversation.home_assistant`) in the Codie's AI language. This is a deliberate exception to ADR-0001, which kept Home Assistant from interpreting sentences. We accept it here because the Character's model, not the user, writes the sentence, and because `timer_start` runs the Command through the built-in agent's recognizer before starting the Timer. A Command the agent would not understand fails straight away, so the Character can rephrase it or tell the user, rather than failing silently when the Timer ends.

The alternatives were storing a deferred Tool call, which needs an arguments schema for every Tool that the Codie's restricted JSON Schema cannot express, and Home Assistant's own timers with `conversation_command`. Home Assistant's cancel and status intents ignore command timers, so the Character could not cancel one or say how long is left.

## Consequences

- The integration keeps its own Timers and its own cancel and status Tool. They do not appear in Home Assistant's timer system or on other voice satellites.
- A plain countdown, a Timer without a Command, lives in the same Timers: it has no check and runs nothing, and when it ends the Character says so with an Announcement. A Command that is itself a timer sentence is refused. Home Assistant's own timers started on the Codie's device by something else, such as an automation calling `HassStartTimer`, are handled too: the Codie is registered as a timer device and says the same message when one finishes. Those stay in Home Assistant's timer system, so the Timer Tools do not cancel them or report their time left.
- The Command is always run by the built-in agent, even when the user's default agent is an LLM, because that is the agent the check ran against. If the built-in agent does not support the Codie's AI language, the Timer Tools are unavailable.
- A Command naming no entity or area Home Assistant knows, such as a misheard name, fails at `timer_start`. The Expose list itself is checked when the Command runs, not when the Timer starts: the recognizer matches unexposed names too, so a Command for an unexposed entity starts and fails when the Timer ends.
- We get the Command's result back, which Home Assistant's own timers discard.
- The check accepts a Command that matches an intent or one of the user's sentence triggers, because both run when the Timer ends.
- The check relies on the built-in agent's recognizer, which Home Assistant does not export as a public helper. If it is missing, the Timer Tools are unavailable; a Timer is never started unchecked.
