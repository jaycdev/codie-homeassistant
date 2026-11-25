# CODE27 Codie for Home Assistant

Talk to your Codie's Character to control your home. Say "Hey Codie, turn on my bedroom TV" or "Hey Codie, set the AC to 24°C", and the Character in the active Slot makes it happen. Home Assistant automations can also make the Character speak.

The Character's own model on the Codie understands what you say. It calls a Tool that this integration offers. Home Assistant runs the Tool, and the Character phrases the reply in its own voice. Home Assistant never hears your words, only the Tool call.

> [!WARNING]
> **The Expose list is the only safety control.** The Character can operate every Exposed entity without asking you first. Nothing is singled out and no Tool asks for confirmation. Don't expose locks, alarms, garage doors or anything else unless you accept the Character operating it unprompted. That includes a misheard request, or one made by anyone in earshot of the Codie.

## Requirements

- A Codie on software 1.6.0, on the same local network as Home Assistant, with a user signed in.
- Home Assistant 2026.11.2 or later.
- [HACS](https://hacs.xyz/).

## Installation

1. In HACS, open the menu (⋮) and select **Custom repositories**.
2. Add `https://github.com/jaycdev/codie-homeassistant` with the type **Integration**.
3. Search HACS for **CODE27 Codie**, select **Download**, and restart Home Assistant.

## Pairing

Pairing is done once per Codie. It gives Home Assistant lasting credentials for that Codie.

1. Wake the Codie and make sure a user is signed in on it.
2. In Home Assistant, go to **Settings → Devices & services → Add integration** and choose **CODE27 Codie**.
3. Enter the Codie's IP address or hostname (you can find it in your router) and the port. Leave the port at 8081 unless you changed it.
4. The Codie shows a six-digit code. Enter it in Home Assistant within 5 minutes.

The Codie is added as a device with these entities:

- **Connected** (binary sensor): on while Home Assistant is connected to the Codie and serving its Tools. While it is off, new conversations with the Character have no home-control Tools.
- **Announce** (notify): see [Announcements](#announcements).
- **Active Character** (sensor): the name of the Character in the active Slot. Its `slot` attribute is the Slot's number.
- **Character** (select): one option per Slot, named after its Character, and the active one selected. Choosing another switches the Codie to that Slot; the Codie then loads the Character, which can take a moment to show. When two Slots hold Characters with the same name, the options add the Slot (`Aiko (Slot 2)`), and a Slot whose Character has no name is shown as `Slot 4`. Both are unavailable while the Codie does not report its Characters, and so is Voice volume, which is read with them.
- **Voice volume** (number, 0–100): how loud the Character speaks, Announcements included. It follows the active Slot: it shows and sets the volume of the Character in that Slot, so it changes when you switch Characters, from the Codie or with the Character select.
- **Music volume** (number, 0–100): how loud the Codie plays music. One level for the whole Codie. It works without the music page open. The Codie only queues the change, so Home Assistant asks it for up to three seconds whether the change ran. The action succeeds once the Codie reports that it applied the change. The Codie has 11 volume steps (0, 9, 18, 27 … 90, 100), so Home Assistant sets the step nearest the level you choose: 40 becomes 36, and 41 becomes 45. It fails if the Codie reports an error, or says nothing within three seconds and its Music volume is not in that step. Unavailable while the Codie's music player is not ready.
- **Music** (media player): the Codie's music. It is playing, paused, buffering or idle, and shows the track's title, artist, album, length and position; the Codie reports no artwork. The `music_mode` attribute says whether the Codie is in Music mode. Play, pause, stop, next and previous only work in Music mode: open the music player on the Codie first, or the action fails and says so. Its volume is the Music volume, works the same way and needs no Music mode; volume up and down move one of the Codie's 11 steps. Like the Music volume, each control waits up to three seconds for the Codie to report that it ran. If the Codie says nothing in that time, the action still succeeds when the player shows the change (it plays after play, or the track changed after next), and fails otherwise. Unavailable while the Codie's music player is not ready.
- **Screen** (switch): turns the Codie's screen on and off. It is unknown when the Codie's two screens disagree. If the Codie sent the change but could not confirm it, the action fails.
- **Screen brightness** (number, 0–100 %, configuration): unavailable while the Codie reports that its brightness cannot be set.
- **Side lights** (switch, configuration): the LED strip whenever the Codie is not in Music mode.
- **Music side lights** (switch, configuration): the LED strip while the Codie is in Music mode. It is switched separately from Side lights.

Home Assistant reads these settings every 30 seconds and again right after it changes one. They are all unavailable while the Codie can't be reached. If the Codie fails to give one setting, only that one is unavailable. The Codie works without them, so a Codie whose settings can't be read still starts.

You can pair several Codies, one entry each. Removing an entry also removes its Tools from the Codie.

If the Codie stops accepting Home Assistant, for example because another user signed in on the Codie or its pairing was reset, Home Assistant asks you to pair again: look for it under **Settings → Devices & services**. Switching the Character's Slot does not need a new Pairing: the Tools belong to the signed-in user, not to a Slot.

## What the Character can do

The Character can call these Tools. Each one runs one of Home Assistant's own Assist tools, so it works on the same devices and understands the same names, areas and aliases that Assist does.

| Tool | What it does | Assist tool |
| --- | --- | --- |
| `get_home_state` | Reads the state of your Exposed entities and areas. The Character calls it to answer questions about the home, and when it is unsure of a device's name. | GetLiveContext |
| `turn_on` | Turns on a device, or every device of a kind in an area. | HassTurnOn |
| `turn_off` | Turns off a device, or every device of a kind in an area. | HassTurnOff |
| `light_set` | Sets a light's brightness or colour. | HassLightSet |
| `climate_set_temp` | Sets the target temperature of a thermostat or AC. | HassClimateSetTemperature |
| `set_position` | Sets the position of a cover or valve. | HassSetPosition |
| `fan_set_speed` | Sets a fan's speed. | HassFanSetSpeed |
| `media_control` | Pauses, resumes, or skips to the next or previous item of a media player, by its `action`. | HassMediaPause, HassMediaUnpause, HassMediaNext, HassMediaPrevious |
| `set_volume` | Sets a media player's volume. | HassSetVolume |
| `media_search_play` | Searches for media and plays it. | HassMediaSearchAndPlay |
| `vacuum_start` | Starts a vacuum. | HassVacuumStart |
| `list_add_item` | Adds an item to a shopping or to-do list. | HassListAddItem |
| `todo_get_items` | Reads the items on a to-do list. | todo_get_items |
| `timer_start` | Starts a [Timer](#timers): a Command to run after a delay. | None: the integration runs it |
| `timer_control` | Cancels a running Timer, or says how much time is left on one or all of them, by its `action`. | None: the integration runs it |

The list is fixed. A Tool is left out if your Home Assistant doesn't have its Assist tool, or if the tool's parameters don't fit the Codie's limits. Nothing replaces it. The Timer Tools are left out too if Home Assistant's built-in agent doesn't support the Codie's AI language. Exposed scripts are not Tools of their own: the Character runs them through `turn_on`, without their fields, and `turn_on`'s description lists their names so the Character can find them. The Codie allows at most 16 Tools per integration, and a list that changed with your setup would make the Character's abilities hard to predict. The catalog has 15 Tools. One place is left free on purpose, for a Tool added later.

The Character keeps the Tools it had when a conversation started. After a change, such as a Home Assistant restart or upgrade, or an integration that adds Assist tools, start a new conversation to get the new Tools.

The Character understands and replies in the Codie's AI language, set on the Codie. Home Assistant reads it when the integration starts. If you change it, reload the integration.

## Timers

A Timer makes the Character act on the home later: "Hey Codie, turn off the kitchen lamp in 20 minutes". The Character sets it with `timer_start`, giving a **Command** (a short home-control sentence such as "turn off the kitchen lamp"), a delay and, if it wants, a name. When the Timer ends, Home Assistant runs the Command.

Timers are not the Codie's own Alarms and Reminders. Those belong to the Character, are set on the Codie, and can only speak. A Timer acts on the home. A plain countdown that only rings is not a Timer here.

- **The Command is checked when the Timer is set.** Home Assistant's built-in agent must understand it as one of its own sentences or as one of your sentence triggers, in the Codie's AI language. It must also name entities or areas Home Assistant knows, so a misheard name fails too. If either check fails, the Tool fails at once and the Character can rephrase it or tell you. Whether those entities are Exposed is checked later, when the Command runs.
- **The built-in agent runs it.** Whatever your default Assist agent is, the Command always runs through the built-in one, because that is the agent that checked it. If the built-in agent doesn't support the Codie's AI language, or Home Assistant no longer offers the recognizer the check needs, the Timer Tools are unavailable and the Character doesn't have them. A Timer is never started unchecked.
- **Names.** Without a `name`, the Command names the Timer. Names are matched without regard to case, and a part of a name works if it fits only one Timer. Two Timers can't share a name.
- **Limits.** A Timer lasts more than zero and at most 24 hours (`hours`, `minutes` and `seconds` add up). A Codie runs at most 10 Timers at once.
- **Cancel and status.** `timer_control` with `status` lists every running Timer with the time left and its Command, or one Timer by name. With `cancel`, it stops the named Timer, or the only one if just one is running. If several are running and none is named, the Character is told to ask which.
- **Restarts.** Timers belong to the Codie they were set on and are stored by Home Assistant. They resume with their remaining time after a restart or a reload of the integration. A Timer that ended while Home Assistant was down is dropped with a warning in the log, and its Command is never run late. Removing the Codie from Home Assistant deletes its Timers.
- **Failures are announced.** If the Command fails when the Timer ends, for example because the entity is no longer Exposed, the Character says so with an immediate [Announcement](#announcements) such as "I couldn't turn off the kitchen lamp: ...". A Command that works is silent. If the Codie can't be reached, the failure is only written to the log.

Timers are kept by this integration, not by Home Assistant's own timers, so they don't appear in Home Assistant's timer list or on other voice satellites.

## Seeing what the Character did

Nothing else in Home Assistant records the Tool calls the Character makes, so the integration does.

**Event.** Every Tool call fires a `codie_tool_called` event once it has finished, whether it succeeded or failed. That includes the read Tools (`get_home_state`, `todo_get_items`) and calls that failed because they timed out. A call the Codie abandoned by disconnecting has no known outcome and fires nothing. The event data:

- `device_id`: The Codie's device id, to filter on when you have several Codies. `null` if the device is missing.
- `tool`: The Tool's name, such as `turn_off`. It is whatever name the Codie sent, so it can be one that does not exist.
- `assist_tool`: The Assist tool the Tool maps to, such as `HassTurnOff`. For `media_control` it is the one the call's `action` selects. `null` for the Timer Tools, for an unknown Tool and for an invalid `action`.
- `arguments`: The arguments the Character gave, as received.
- `success`: `true` if the call succeeded.
- `error`: The error code if it failed, otherwise `null`. For example `target_not_found`, `ambiguous_target`, `invalid_arguments`, `tool_unavailable`, `unknown_tool`, `timeout` or `internal_error`.
- `entity_ids`: The entities the call acted on, as a list, when Home Assistant says which. Empty for a failed call and for Tools that do not act on entities.

An automation can react to a call, for example to notify you whenever the Character turns something off:

```yaml
triggers:
  - trigger: event
    event_type: codie_tool_called
    event_data:
      tool: turn_off
      success: true
```

**Logbook.** The calls that change something also appear in the logbook, listed under the Codie whose Character made the call, such as "turned off Kitchen light" or "could not turn off Garage light (target_not_found)". The entry is linked to that Codie's device and, if the call acted on exactly one entity, to that entity's history too. Home Assistant can only link a single entity to an entry, so a call that acted on several has one entry under the Codie, naming them all. Calls that only read are left out so they do not flood the logbook: the read Tools, and `timer_control` with `status`. The logbook can only describe every event of one type or none, so the integration fires a second event, `codie_tool_logged`, for the calls it shows. It has the same data as `codie_tool_called`, plus `entity_id` when the call acted on exactly one entity. Use `codie_tool_called` in automations.

## Exposure controls access

The Character only sees and controls **Exposed entities**: the entities you expose to Assist. Manage them in **Settings → Voice assistants → Expose**. The Character can't see an entity you haven't exposed, and asking it to control one does nothing. When a request names something that isn't exposed, the Character is told which exposed names it may use instead.

To make requests easier to match, give entities clear names, assign them to areas, and add aliases (open the entity, then **Voice assistants**).

Remember the [warning above](#code27-codie-for-home-assistant): anything exposed can be operated by the Character without confirmation.

## Screen switch and Assist

> [!WARNING]
> If you expose the **Screen** switch to Assist, the Character can turn its own screen off, for example when you say "Hey Codie, turn off your screen". A Codie with its screen off blocks conversations and Announcements. Only Home Assistant or the Codie itself can turn the screen back on. Leave the Screen switch unexposed unless you accept this.

## Announcements

An Announcement is text the Character speaks exactly as given, without its model deciding what to say. There are two kinds.

| | Announcement | Queued announcement |
| --- | --- | --- |
| When it is spoken | At once, cutting off whatever the Character is doing | Once the Character is idle |
| Remembered by the Character | No | Yes, saved to its chat history as something it said |
| Length | 1–16000 characters | 1–200 characters. Longer messages are rejected, never cut short |
| How to send it | `notify.send_message`, or `codie.announce` | `codie.announce` with `queue: true` |

Send an Announcement with the Codie's Announce entity:

```yaml
action: notify.send_message
target:
  entity_id: notify.codie_192_168_1_50_announce  # your Codie's Announce entity
data:
  message: The washing is done
```

Or with the `codie.announce` action, which also sends a Queued announcement:

```yaml
action: codie.announce
target:
  device_id: YOUR_CODIE_DEVICE_ID  # pick the Codie in the action editor
data:
  message: The washing is done
  queue: true
```

The Character speaks the text in whatever language it is written. Nothing is translated.

The action returns as soon as the Codie accepts the message. It does not wait for the Character to finish speaking. The Codie's screen is never woken: if the Codie is asleep or locked, the action fails with an error that names the reason. Wake the Codie and try again.

### Blueprints

These blueprints make automations for common Announcements. Import one, then create an automation from it under **Settings → Automations & scenes → Blueprints**, and pick your Codie and sensors.

| Blueprint | What it does | |
| --- | --- | --- |
| Doorbell | An Announcement the moment a doorbell binary sensor turns on, or a doorbell event entity is pressed. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fjaycdev%2Fcodie-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fcodie%2Fdoorbell.yaml) |
| Appliance finished | A Queued announcement once a power sensor stays below a threshold for a while, such as a washing machine or dishwasher at the end of its cycle. A sensor coming back after a restart doesn't count as finishing. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fjaycdev%2Fcodie-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fcodie%2Fappliance_finished.yaml) |
| Morning briefing | A Queued announcement with today's weather and, optionally, today's calendar events. Given at a chosen time, or at the first presence detected after it. It names fewer events when needed to stay within 200 characters. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fjaycdev%2Fcodie-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fcodie%2Fmorning_briefing.yaml) |

## Troubleshooting

- **Connected is off.** Check that the Codie is awake and on the network. Home Assistant keeps retrying with a growing delay.
- **"The clock of Home Assistant and the Codie differ too much" in the log.** Every request is signed with the time. Check that the time is correct on both.
- **The Character doesn't know a device.** Check that it is exposed, then start a new conversation. Calling `get_home_state` shows what the Character can see.

For more detail, turn on debug logging:

```yaml
logger:
  logs:
    custom_components.codie: debug
```

When you report a problem, attach the integration's diagnostics: open the Codie's entry under **Settings → Devices & services**, open its menu (⋮) and select **Download diagnostics**. The file has the connection state and why the last connection ended, the Tools registered on the Codie and why any were left out, the latest settings and music playback, the running Timers with their Commands, and the Codie's system details. The credentials, the IDs of the Codie and its user, and the username are redacted.

Never share the integration's stored credentials or a request's `X-Local-*` headers.
