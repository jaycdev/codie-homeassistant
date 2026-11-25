"""Every Tool call is an event, and the calls that change something are logbook entries."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import async_capture_events, async_mock_service

from custom_components.codie.const import DOMAIN
from custom_components.codie.logbook import async_describe_events
from custom_components.codie.tool_events import EVENT_TOOL_CALLED, EVENT_TOOL_LOGGED, ToolRecorder

from .fake_codie import FakeCodie
from .test_tool_list import add_entity, setup_assist
from .test_tools import INTEGRATION_ID, setup, wait_connected


async def _ready(hass: HomeAssistant, fake: FakeCodie) -> str:
    """Set up the Codie with Assist loaded. Returns the Codie's device id."""
    await setup_assist(hass)
    entry = await setup(hass, fake)
    await wait_connected(hass, fake, True)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, fake.machine_id), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_a_successful_call_fires_an_event(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    device_id = await _ready(hass, fake_codie)
    lamp = add_entity(hass, "light", "kitchen_lamp", True)
    async_mock_service(hass, "light", "turn_off")
    events = async_capture_events(hass, EVENT_TOOL_CALLED)

    result = await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "kitchen_lamp"})
    await hass.async_block_till_done()

    assert result["status"] == "succeeded"
    assert [e.data for e in events] == [
        {
            "device_id": device_id,
            "tool": "turn_off",
            "assist_tool": "HassTurnOff",
            "arguments": {"name": "kitchen_lamp"},
            "success": True,
            "error": None,
            "entity_ids": [lamp],
        }
    ]


async def test_a_failed_call_fires_an_event_with_its_code(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    device_id = await _ready(hass, fake_codie)
    add_entity(hass, "light", "secret_lamp", False)
    async_mock_service(hass, "light", "turn_off")
    events = async_capture_events(hass, EVENT_TOOL_CALLED)

    result = await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "secret_lamp"})
    await hass.async_block_till_done()

    assert result["status"] == "failed"
    assert result["code"] == "target_not_found"
    assert [e.data for e in events] == [
        {
            "device_id": device_id,
            "tool": "turn_off",
            "assist_tool": "HassTurnOff",
            "arguments": {"name": "secret_lamp"},
            "success": False,
            "error": "target_not_found",
            "entity_ids": [],
        }
    ]


async def test_an_unknown_tool_is_recorded_without_an_assist_tool(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    events = async_capture_events(hass, EVENT_TOOL_CALLED)
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    await fake_codie.invoke(INTEGRATION_ID, "nope", {})
    await hass.async_block_till_done()

    assert events[0].data["assist_tool"] is None
    assert events[0].data["error"] == "unknown_tool"
    assert logged == []


async def test_a_combined_tool_reports_the_assist_tool_its_action_maps_to(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    events = async_capture_events(hass, EVENT_TOOL_CALLED)

    await fake_codie.invoke(INTEGRATION_ID, "media_control", {"action": "resume", "name": "tv"})
    await fake_codie.invoke(INTEGRATION_ID, "media_control", {"action": "dance", "name": "tv"})
    await hass.async_block_till_done()

    assert [(e.data["assist_tool"], e.data["error"]) for e in events] == [
        ("HassMediaUnpause", "target_not_found"),
        (None, "invalid_arguments"),
    ]


async def test_a_timer_tool_has_no_assist_tool(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await _ready(hass, fake_codie)
    events = async_capture_events(hass, EVENT_TOOL_CALLED)

    await fake_codie.invoke(INTEGRATION_ID, "timer_control", {"action": "status"})
    await hass.async_block_till_done()

    assert events[0].data["tool"] == "timer_control"
    assert events[0].data["assist_tool"] is None


async def test_a_read_tool_is_an_event_but_not_a_logbook_entry(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    device_id = await _ready(hass, fake_codie)
    called = async_capture_events(hass, EVENT_TOOL_CALLED)
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    await fake_codie.invoke(INTEGRATION_ID, "get_home_state", {})
    await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "nothing"})
    await hass.async_block_till_done()

    assert [e.data["tool"] for e in called] == ["get_home_state", "turn_off"]
    assert called[0].data["device_id"] == device_id
    assert [e.data["tool"] for e in logged] == ["turn_off"]


async def test_a_timeout_is_recorded_as_a_failure(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    async def slow(*args: Any) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    with patch("custom_components.codie.async_call_tool", slow):
        await _ready(hass, fake_codie)
        events = async_capture_events(hass, EVENT_TOOL_CALLED)
        await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "x"}, timeout_ms=800)
        await hass.async_block_till_done()

    assert [(e.data["success"], e.data["error"]) for e in events] == [(False, "timeout")]


async def test_a_crash_is_recorded_as_an_internal_error(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    async def crash(*args: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    with patch("custom_components.codie.async_call_tool", crash):
        await _ready(hass, fake_codie)
        events = async_capture_events(hass, EVENT_TOOL_CALLED)
        result = await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "x"})
        await hass.async_block_till_done()

    assert result["code"] == "internal_error"
    assert [(e.data["success"], e.data["error"]) for e in events] == [(False, "internal_error")]


async def test_a_recording_failure_does_not_fail_the_call(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    add_entity(hass, "light", "kitchen_lamp", True)
    async_mock_service(hass, "light", "turn_off")

    with patch.object(ToolRecorder, "_fire", side_effect=RuntimeError("x")):
        result = await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "kitchen_lamp"})

    assert result["status"] == "succeeded"


async def _describe(hass: HomeAssistant, event: Event[Any]) -> dict[str, Any]:
    """What the logbook shows for `event`, through the callback the platform registers."""
    registered: dict[str, Any] = {}

    def register(domain: str, event_name: str, describe: Any) -> None:
        registered[event_name] = (domain, describe)

    async_describe_events(hass, register)
    domain, describe = registered[str(event.event_type)]
    assert domain == DOMAIN
    return dict(describe(event))


async def test_the_logbook_describes_a_call_and_links_the_entity(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    lamp = add_entity(hass, "light", "Kitchen light", True)
    async_mock_service(hass, "light", "turn_off")
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "Kitchen light"})
    await hass.async_block_till_done()

    entry = await _describe(hass, logged[0])
    assert entry["name"] == "Codie"
    assert entry["message"] == "turned off Kitchen light"
    assert entry["entity_id"] == lamp


async def test_the_logbook_describes_a_failed_call(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "Garage light"})
    await hass.async_block_till_done()

    entry = await _describe(hass, logged[0])
    assert entry["message"] == "could not turn off Garage light (target_not_found)"
    assert "entity_id" not in entry


async def test_the_logbook_names_the_action_of_a_combined_tool(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    tv = add_entity(hass, "media_player", "Jay TV", True)
    # Playing, and able to do all of it: the intents only match such a player.
    hass.states.async_set(
        tv, "playing", {"friendly_name": "Jay TV", "supported_features": 0xFFFFFF}
    )
    async_mock_service(hass, "media_player", "media_pause")
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    await fake_codie.invoke(INTEGRATION_ID, "media_control", {"action": "pause", "name": "Jay TV"})
    await hass.async_block_till_done()

    entry = await _describe(hass, logged[0])
    assert entry["message"] == "paused Jay TV"
    assert entry["entity_id"] == tv


async def test_the_logged_event_has_the_entity_id_the_logbook_filters_on(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    """The logbook's history query matches only a single `entity_id` string in the event data."""
    await _ready(hass, fake_codie)
    lamp = add_entity(hass, "light", "kitchen_lamp", True)
    add_entity(hass, "light", "hall_lamp", True)
    async_mock_service(hass, "light", "turn_off")
    called = async_capture_events(hass, EVENT_TOOL_CALLED)
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "kitchen_lamp"})
    await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "nothing"})
    await hass.async_block_till_done()

    assert logged[0].data["entity_id"] == lamp
    assert "entity_id" not in logged[1].data
    assert "entity_id" not in called[0].data


async def test_a_call_on_several_entities_is_one_entry_without_a_single_link(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    lamps = [add_entity(hass, "light", name, True) for name in ("lamp_a", "lamp_b")]
    for lamp in lamps:
        hass.states.async_set(lamp, "on", {"friendly_name": lamp.split(".")[1]})
    async_mock_service(hass, "light", "turn_off")
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    with patch.object(ToolRecorder, "_affected_entities", return_value=lamps):
        await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "lamp_a"})
    await hass.async_block_till_done()

    assert logged[0].data["entity_ids"] == lamps
    assert "entity_id" not in logged[0].data
    entry = await _describe(hass, logged[0])
    assert entry["message"] == "turned off lamp_a, lamp_b"
    assert "entity_id" not in entry


async def test_a_read_action_of_a_combined_tool_is_not_a_logbook_entry(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    called = async_capture_events(hass, EVENT_TOOL_CALLED)
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    await fake_codie.invoke(INTEGRATION_ID, "timer_control", {"action": "status"})
    await fake_codie.invoke(INTEGRATION_ID, "timer_control", {"action": "cancel"})
    await hass.async_block_till_done()

    assert len(called) == 2
    assert [e.data["arguments"] for e in logged] == [{"action": "cancel"}]


async def test_arguments_that_are_not_an_object_do_not_break_recording(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await _ready(hass, fake_codie)
    called = async_capture_events(hass, EVENT_TOOL_CALLED)
    logged = async_capture_events(hass, EVENT_TOOL_LOGGED)

    result = await fake_codie.invoke(INTEGRATION_ID, "turn_off", ["kitchen"])  # type: ignore[arg-type]
    await hass.async_block_till_done()

    assert result["status"] == "failed"
    assert called[0].data["arguments"] == {}
    entry = await _describe(hass, logged[0])
    assert entry["message"].startswith("could not turn off something")
    # The logbook also copes with an event that was stored with such arguments.
    event = Event(EVENT_TOOL_LOGGED, {**logged[0].data, "arguments": ["kitchen"]})
    assert (await _describe(hass, event))["message"].startswith("could not turn off something")
