# CODE27 Codie for Home Assistant

A Home Assistant integration that lets a Codie's Character control the home on request, and lets Home Assistant make the Character speak.

## Language

### The device

**Codie**:
A CODE27 desktop companion device on the local network, paired with Home Assistant.
_Avoid_: CODE27 (the maker), codei, codeihub, the device

**Character**:
The persona in the Codie's active Slot, with its own name and voice; the one that answers when the user says "Hey Codie, turn on my bedroom TV". The wake word is always "Hey Codie", whichever Character is active.
_Avoid_: Codie (when meaning the persona), assistant, agent

**Slot**:
One of the Codie's numbered positions, each holding a Character; exactly one is active at a time.

**Voice volume**:
How loud a Character speaks, Announcements included. Each Slot keeps its own.
_Avoid_: volume (unqualified), TTS volume

**Music volume**:
How loud the Codie plays music. One level for the whole Codie, separate from Voice volume.
_Avoid_: volume (unqualified), system volume

**Music mode**:
The Codie showing its music player instead of the Character.

**Side lights**:
The Codie's LED strip, whenever it is not in Music mode. Switched on and off separately from Music side lights.
_Avoid_: normal side lights, LEDs

**Music side lights**:
The Codie's LED strip while it is in Music mode.

**Alarm**:
The Character's own, set by talking to it, at a clock time. It can only speak; it never acts on the home.

**Reminder**:
The Character's own note to bring something up at a time. Like an Alarm, it can only speak.

**Timer**:
A countdown the Character sets through Home Assistant that carries a Command and runs it when it ends ("turn off the lights in 20 minutes"). Unlike an Alarm, it acts on the home.
_Avoid_: delayed action, scheduled command

**Command**:
A short home-control sentence, written by the Character's model, that a Timer runs when it ends.

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
Text Home Assistant sends for the Character to speak. Unless it is an Interpreted announcement, the Character speaks it as-is, without its model deciding what to say. By default it is immediate: it cuts off whatever the Character is doing and is not remembered by the Character.
_Avoid_: notification, TTS message

**Queued announcement**:
A short Announcement that waits until the Character is idle and is remembered as something the Character said.

**Interpreted announcement**:
An immediate Announcement the Character says in its own words and personality ("Washing machine is finished" becomes "The laundry's done, go get it quick!"), optionally in a manner Home Assistant asks for, such as urgent or brief. It is remembered only when Home Assistant asks, and if the Character cannot say it, the original text is spoken as-is. Shown as "in character" to users.
_Avoid_: rephrased announcement, AI announcement
