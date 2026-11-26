"""The shipped blueprints, run as real automations with `codie.announce` recorded."""

from __future__ import annotations

import shutil
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.codie.const import DOMAIN

BLUEPRINTS = Path(__file__).parent.parent / "blueprints" / "automation" / "codie"
CODIE = {"device_id": "codie_device"}


@pytest.fixture
async def announced(hass: HomeAssistant, tmp_path: Path) -> AsyncGenerator[list[dict[str, Any]]]:
    """Copy the blueprints into a fresh config folder and record every `codie.announce` call."""
    hass.config.config_dir = str(tmp_path)
    shutil.copytree(BLUEPRINTS, tmp_path / "blueprints" / "automation" / "codie")
    calls: list[dict[str, Any]] = []

    async def record(call: ServiceCall) -> None:
        calls.append(dict(call.data))

    hass.services.async_register(DOMAIN, "announce", record)
    assert await async_setup_component(hass, "homeassistant", {})
    yield calls
    # A time trigger keeps its next run scheduled until the automation is turned off.
    if hass.services.has_service("automation", "turn_off"):
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": "all"}, blocking=True
        )


async def use_blueprint(hass: HomeAssistant, name: str, **inputs: Any) -> None:
    config = {
        "alias": "Blueprint",
        "use_blueprint": {"path": f"codie/{name}.yaml", "input": inputs},
    }
    assert await async_setup_component(hass, "automation", {"automation": config})
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("automation") == ["automation.blueprint"]


# Doorbell


async def test_doorbell_binary_sensor_announces_at_once(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    hass.states.async_set("binary_sensor.doorbell", "off")
    await use_blueprint(
        hass, "doorbell", codie=CODIE, doorbell="binary_sensor.doorbell", message="Ding dong"
    )

    hass.states.async_set("binary_sensor.doorbell", "on")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.doorbell", "off")
    await hass.async_block_till_done()

    assert announced == [{"device_id": ["codie_device"], "message": "Ding dong", "queue": False}]


async def test_doorbell_event_entity_announces_every_press(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    hass.states.async_set("event.doorbell", "2026-11-25T08:00:00+00:00")
    await use_blueprint(hass, "doorbell", codie=CODIE, doorbell="event.doorbell")

    hass.states.async_set("event.doorbell", "2026-11-25T09:00:00+00:00")
    await hass.async_block_till_done()
    hass.states.async_set("event.doorbell", "2026-11-25T09:01:00+00:00")
    await hass.async_block_till_done()

    assert [call["message"] for call in announced] == ["Someone is at the door"] * 2


async def test_doorbell_ignores_the_sensor_coming_back(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    hass.states.async_set("event.doorbell", "unavailable")
    await use_blueprint(hass, "doorbell", codie=CODIE, doorbell="event.doorbell")

    hass.states.async_set("event.doorbell", "2026-11-25T09:00:00+00:00")
    await hass.async_block_till_done()

    assert announced == []


async def test_doorbell_ignores_a_binary_sensor_starting_up_on(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    hass.states.async_set("binary_sensor.doorbell", "unknown")
    await use_blueprint(hass, "doorbell", codie=CODIE, doorbell="binary_sensor.doorbell")

    hass.states.async_set("binary_sensor.doorbell", "on")
    await hass.async_block_till_done()

    assert announced == []


# Appliance finished


async def finish_appliance(
    hass: HomeAssistant, from_state: str, minutes: int, **inputs: Any
) -> None:
    """Run the appliance, drop its power below 5 W, and let `minutes` pass."""
    hass.states.async_set("sensor.washer_power", from_state)
    await use_blueprint(
        hass,
        "appliance_finished",
        codie=CODIE,
        power_sensor="sensor.washer_power",
        message="The washing is done",
        **inputs,
    )
    hass.states.async_set("sensor.washer_power", "1.5")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


async def test_appliance_finished_queues_once_power_stays_low(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    await finish_appliance(hass, "450", minutes=6)

    assert announced == [
        {
            "device_id": ["codie_device"],
            "message": "The washing is done",
            "in_character": False,
            "queue": True,
        }
    ]


async def test_appliance_finished_can_say_it_in_character(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    await finish_appliance(hass, "450", minutes=6, in_character=True)

    assert announced == [
        {
            "device_id": ["codie_device"],
            "message": "The washing is done",
            "in_character": True,
            "queue": False,
        }
    ]


async def test_appliance_finished_waits_out_a_pause_in_the_cycle(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    await finish_appliance(hass, "450", minutes=4)
    hass.states.async_set("sensor.washer_power", "450")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()

    assert announced == []


async def test_appliance_finished_ignores_an_idle_appliance_after_a_restart(
    hass: HomeAssistant, announced: list[dict[str, Any]]
) -> None:
    await finish_appliance(hass, "unavailable", minutes=6)

    assert announced == []


# Morning briefing

FORECAST: JsonObjectType = {
    "weather.home": {
        "forecast": [{"condition": "partlycloudy", "temperature": 23.6, "templow": 14.8}]
    }
}
EVENTS = [
    {
        "start": "2026-11-25T09:00:00-08:00",
        "end": "2026-11-25T10:00:00-08:00",
        "summary": "Dentist",
    },
    {"start": "2026-11-25", "end": "2026-11-26", "summary": "Bin day"},
]


@pytest.fixture
def calendar_events(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Answer `weather.get_forecasts` with FORECAST and `calendar.get_events` with the list."""
    events: list[Any] = list(EVENTS)

    async def forecasts(call: ServiceCall) -> ServiceResponse:
        return FORECAST

    async def get_events(call: ServiceCall) -> ServiceResponse:
        return {"calendar.family": {"events": events}}

    hass.services.async_register(
        "weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        "calendar", "get_events", get_events, supports_response=SupportsResponse.ONLY
    )
    return events


async def use_morning_briefing(hass: HomeAssistant, **inputs: Any) -> None:
    await use_blueprint(hass, "morning_briefing", codie=CODIE, weather="weather.home", **inputs)


async def move_to(hass: HomeAssistant, freezer: FrozenDateTimeFactory, time: str) -> None:
    freezer.move_to(f"2026-11-25 {time}-08:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def messages(announced: list[dict[str, Any]]) -> list[str]:
    assert all(call["queue"] and not call["in_character"] for call in announced)
    return [call["message"] for call in announced]


async def test_morning_briefing_at_the_chosen_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
) -> None:
    await move_to(hass, freezer, "06:00:00")
    await use_morning_briefing(hass, calendars=["calendar.family"])

    await move_to(hass, freezer, "07:00:00")

    assert messages(announced) == [
        "Good morning. Today will be partly cloudy, with a high of 24 and a low of 15 degrees. "
        "You have 2 events today: Bin day, all day, and Dentist at 09:00."
    ]


async def test_morning_briefing_can_say_it_in_character(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
) -> None:
    await move_to(hass, freezer, "06:00:00")
    await use_morning_briefing(hass, calendars=["calendar.family"], in_character=True)

    await move_to(hass, freezer, "07:00:00")

    (call,) = announced
    assert call["in_character"] is True
    assert call["queue"] is False
    assert call["message"].startswith("Good morning. Today will be partly cloudy")


async def test_morning_briefing_with_one_event_or_none(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
) -> None:
    await move_to(hass, freezer, "06:00:00")
    await use_morning_briefing(hass, calendars=["calendar.family"], briefing_time="07:00:00")
    del calendar_events[1]
    await move_to(hass, freezer, "07:00:00")
    calendar_events.clear()
    freezer.move_to("2026-11-26 07:00:00-08:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert [message.split("degrees. ")[1] for message in messages(announced)] == [
        "You have 1 event today: Dentist at 09:00.",
        "Nothing is on your calendar today.",
    ]


async def test_morning_briefing_without_calendars_is_weather_only(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
) -> None:
    await move_to(hass, freezer, "06:00:00")
    await use_morning_briefing(hass, greeting="Morning!")

    await move_to(hass, freezer, "07:00:00")

    assert messages(announced) == [
        "Morning! Today will be partly cloudy, with a high of 24 and a low of 15 degrees."
    ]


async def test_morning_briefing_orders_events_by_time_across_offsets(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
) -> None:
    calendar_events[:] = [
        {
            "start": "2026-11-25T12:00:00-08:00",
            "end": "2026-11-25T13:00:00-08:00",
            "summary": "Lunch",
        },
        {
            "start": "2026-11-25T18:30:00+00:00",
            "end": "2026-11-25T19:00:00+00:00",
            "summary": "Call",
        },
    ]
    await move_to(hass, freezer, "06:00:00")
    await use_morning_briefing(hass, calendars=["calendar.family"])

    await move_to(hass, freezer, "07:00:00")

    assert messages(announced)[0].endswith(
        "You have 2 events today: Call at 10:30, and Lunch at 12:00."
    )


async def test_morning_briefing_stays_within_the_queued_limit(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
) -> None:
    calendar_events[0] = {**EVENTS[0], "summary": "Quarterly planning " * 10}
    await move_to(hass, freezer, "06:00:00")
    await use_morning_briefing(hass, calendars=["calendar.family"])

    await move_to(hass, freezer, "07:00:00")

    (message,) = messages(announced)
    assert len(message) <= 200
    assert message.startswith("Good morning. Today will be partly cloudy")


async def test_morning_briefing_waits_for_the_first_presence(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
) -> None:
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await move_to(hass, freezer, "05:00:00")
    await use_morning_briefing(hass, presence_sensors=["binary_sensor.kitchen_motion"])

    async def motion(time: str) -> None:
        await move_to(hass, freezer, time)
        hass.states.async_set("binary_sensor.kitchen_motion", "on")
        await hass.async_block_till_done()
        hass.states.async_set("binary_sensor.kitchen_motion", "off")
        await hass.async_block_till_done()

    await motion("06:30:00")  # before the briefing time
    await move_to(hass, freezer, "07:00:00")  # the time alone does not brief
    assert announced == []

    await motion("07:20:00")
    await motion("07:40:00")  # once a day
    assert len(announced) == 1

    await motion("13:00:00")  # past the latest time
    assert len(announced) == 1


# Every blueprint


@pytest.mark.parametrize("path", sorted(BLUEPRINTS.glob("*.yaml")), ids=lambda path: path.stem)
def test_blueprint_offers_the_same_codies_as_announce(path: Path) -> None:
    """The Codie input picks exactly what `codie.announce` accepts as its target."""
    services = load_yaml_dict(
        Path(__file__).parent.parent / "custom_components" / "codie" / "services.yaml"
    )
    blueprint = load_yaml_dict(path)["blueprint"]
    assert blueprint["input"]["codie"]["selector"] == {"target": services["announce"]["target"]}
    assert blueprint["source_url"] == (
        f"https://github.com/jaycdev/codie-homeassistant/blob/main/blueprints/automation/codie/{path.name}"
    )


@pytest.mark.parametrize("name", ["appliance_finished", "morning_briefing"])
def test_blueprint_offers_in_character_off_by_default(name: str) -> None:
    """Existing automations sound the same after an update; the doorbell is too slow for it."""
    inputs = load_yaml_dict(BLUEPRINTS / f"{name}.yaml")["blueprint"]["input"]
    assert inputs["in_character"]["default"] is False
    assert inputs["in_character"]["selector"] == {"boolean": None}


def test_the_doorbell_blueprint_has_no_in_character_input() -> None:
    inputs = load_yaml_dict(BLUEPRINTS / "doorbell.yaml")["blueprint"]["input"]
    assert "in_character" not in inputs


@pytest.mark.parametrize("path", sorted(BLUEPRINTS.glob("*.yaml")), ids=lambda path: path.stem)
def test_blueprint_hardcodes_no_entity(path: Path) -> None:
    """Every entity is picked by the user: an entity input defaults to none at most."""
    for name, field in load_yaml_dict(path)["blueprint"]["input"].items():
        if {"entity", "target", "device"} & field["selector"].keys():
            assert field.get("default", []) == [], name
