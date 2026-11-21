# CODE27 Codie for Home Assistant

A Home Assistant integration that lets a Codie's Character control the home on request, and lets Home Assistant make the Character speak.

## Language

### The device

**Codie**:
A CODE27 desktop companion device on the local network, paired with Home Assistant.
_Avoid_: CODE27 (the maker), codei, codeihub, the device

**Character**:
The persona in the Codie's active Slot, with its own name and voice; the one the user addresses ("Aria, turn on my bedroom TV").
_Avoid_: Codie (when meaning the persona), assistant, agent

**Slot**:
One of the Codie's numbered positions, each holding a Character; exactly one is active at a time.

**Pairing**:
The one-time exchange in which the Codie displays a code, the user enters it in Home Assistant, and Home Assistant receives lasting credentials for that Codie.

### Home control

**Exposed entity**:
A Home Assistant entity the user has exposed to Assist; the only entities a Character may see or control.
_Avoid_: allowed device, accessible device, shared entity

**Tool**:
An action Home Assistant offers to the Character's model, which the model may call during a conversation.
_Avoid_: function, skill, command

**Announcement**:
Text Home Assistant sends for the Character to speak as-is, without the Character's model deciding what to say. By default it is immediate: it cuts off whatever the Character is doing and is not remembered by the Character.
_Avoid_: notification, TTS message

**Queued announcement**:
A short Announcement that waits until the Character is idle and is remembered as something the Character said.
