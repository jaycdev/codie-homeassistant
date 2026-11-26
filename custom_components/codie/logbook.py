"""Describes the Tool calls the Character made and the Announcements it said, for the logbook."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .announce import (
    EVENT_ANNOUNCEMENT_FINISHED,
    OUTCOME_FALLBACK,
    OUTCOME_IN_CHARACTER,
    OUTCOME_INTERRUPTED,
)
from .const import DOMAIN
from .tool_events import EVENT_TOOL_LOGGED

# What the Character did, as "<Codie> <past>" and, if it failed, "<Codie> could not <base>".
# `{target}` is what it acted on. A combined Tool has a phrase for each action.
_PHRASES: dict[str, tuple[str, str]] = {
    "turn_on": ("turned on {target}", "turn on {target}"),
    "turn_off": ("turned off {target}", "turn off {target}"),
    "light_set": ("set the light of {target}", "set the light of {target}"),
    "climate_set_temp": ("set the temperature of {target}", "set the temperature of {target}"),
    "set_position": ("set the position of {target}", "set the position of {target}"),
    "fan_set_speed": ("set the fan speed of {target}", "set the fan speed of {target}"),
    "media_control:pause": ("paused {target}", "pause {target}"),
    "media_control:resume": ("resumed {target}", "resume {target}"),
    "media_control:next": (
        "skipped to the next item on {target}",
        "skip {target} to the next item",
    ),
    "media_control:previous": (
        "went back to the previous item on {target}",
        "take {target} back to the previous item",
    ),
    "set_volume": ("set the volume of {target}", "set the volume of {target}"),
    "media_search_play": ("played media on {target}", "play media on {target}"),
    "vacuum_start": ("started {target}", "start {target}"),
    "list_add_item": ("added an item to {target}", "add an item to {target}"),
    "timer_start": ("started a Timer", "start a Timer"),
    "timer_control:cancel": ("cancelled a Timer", "cancel a Timer"),
    "timer_control:status": ("checked its Timers", "check its Timers"),
}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event[Any]], dict[str, Any]]], None],
) -> None:
    """Teach the logbook to describe the Tool calls and the Interpreted announcements."""

    @callback
    def describe(event: Event[Any]) -> dict[str, Any]:
        data = event.data
        arguments = data["arguments"] if isinstance(data["arguments"], dict) else {}
        device = dr.async_get(hass).async_get(data["device_id"]) if data["device_id"] else None
        entity_ids: list[str] = data["entity_ids"]
        names = [_name(hass, entity_id) for entity_id in entity_ids]
        past, base = _PHRASES.get(
            _phrase_key(data["tool"], arguments), (f"called {data['tool']}", f"call {data['tool']}")
        )
        target = ", ".join(names) or _asked_for(arguments)
        if data["success"]:
            message = past.format(target=target)
        else:
            message = f"could not {base.format(target=target)} ({data['error']})"
        described = {
            LOGBOOK_ENTRY_NAME: (device and (device.name_by_user or device.name)) or "Codie",
            LOGBOOK_ENTRY_MESSAGE: message,
        }
        # The logbook links one entity; a call that acted on several has none to prefer.
        if entity_id := data.get("entity_id"):
            described[LOGBOOK_ENTRY_ENTITY_ID] = entity_id
        return described

    async_describe_event(DOMAIN, EVENT_TOOL_LOGGED, describe)

    @callback
    def describe_announcement(event: Event[Any]) -> dict[str, Any]:
        data = event.data
        device = dr.async_get(hass).async_get(data["device_id"]) if data["device_id"] else None
        said = data["message"]
        if data["outcome"] == OUTCOME_IN_CHARACTER:
            message = f'said "{said}" in its own words: "{data["reply"]}"'
        elif data["outcome"] == OUTCOME_FALLBACK:
            message = f'could not say "{said}" in its own words and said it as given'
        elif data["outcome"] == OUTCOME_INTERRUPTED:
            message = f'was interrupted before saying "{said}" in its own words'
        else:
            message = f'could not say "{said}" in its own words or as given'
        return {
            LOGBOOK_ENTRY_NAME: (device and (device.name_by_user or device.name)) or "Codie",
            LOGBOOK_ENTRY_MESSAGE: message,
        }

    async_describe_event(DOMAIN, EVENT_ANNOUNCEMENT_FINISHED, describe_announcement)


def _phrase_key(tool: str, arguments: dict[str, Any]) -> str:
    key = f"{tool}:{arguments.get('action')}"
    return key if key in _PHRASES else tool


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return str(state.name) if state else entity_id


def _asked_for(arguments: dict[str, Any]) -> str:
    """What the Character named, for a call that acted on nothing."""
    for key in ("name", "area", "floor"):
        if isinstance(value := arguments.get(key), str) and value:
            return value
    return "something"
