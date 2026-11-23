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
- **Voice volume** (number, 0–100): how loud the Character speaks, Announcements included. It follows the active Slot: it shows and sets the volume of the Character in that Slot, so it changes when you switch Slots.
- **Music volume** (number, 0–100): how loud the Codie plays music. One level for the whole Codie. It works without the music page open. The Codie only queues the change, so Home Assistant asks it for up to three seconds whether the change ran. The action succeeds once the Codie reports that it applied the change. The Codie has 11 volume steps (0, 9, 18, 27 … 90, 100), so Home Assistant sets the step nearest the level you choose: 40 becomes 36, and 41 becomes 45. It fails if the Codie reports an error, or says nothing within three seconds and its Music volume is not in that step. Unavailable while the Codie's music player is not ready.
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
| `media_pause` | Pauses a media player. | HassMediaPause |
| `media_unpause` | Resumes a media player. | HassMediaUnpause |
| `media_next` | Skips to the next track. | HassMediaNext |
| `media_previous` | Goes back to the previous track. | HassMediaPrevious |
| `set_volume` | Sets a media player's volume. | HassSetVolume |
| `media_search_play` | Searches for media and plays it. | HassMediaSearchAndPlay |
| `vacuum_start` | Starts a vacuum. | HassVacuumStart |
| `list_add_item` | Adds an item to a shopping or to-do list. | HassListAddItem |
| `todo_get_items` | Reads the items on a to-do list. | todo_get_items |

The list is fixed. A Tool is left out if your Home Assistant doesn't have its Assist tool, or if the tool's parameters don't fit the Codie's limits. Nothing replaces it. Exposed scripts are not Tools. The Codie allows at most 16 Tools per integration, and a list that changed with your setup would make the Character's abilities hard to predict.

The Character keeps the Tools it had when a conversation started. After a change, such as a Home Assistant restart or upgrade, or an integration that adds Assist tools, start a new conversation to get the new Tools.

The Character understands and replies in the Codie's AI language, set on the Codie. Home Assistant reads it when the integration starts. If you change it, reload the integration.

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

When you report a problem, never share the integration's stored credentials or a request's `X-Local-*` headers.
