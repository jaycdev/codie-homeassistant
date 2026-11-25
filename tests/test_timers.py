"""Timers: the Tools, the Command check, running, limits and restarts, against the fake Codie."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.conversation import ConversationResult
from homeassistant.components.conversation.default_agent import DefaultAgent
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed, async_mock_service

from custom_components.codie.const import DOMAIN
from custom_components.codie.errors import ToolError
from custom_components.codie.timers import CodieTimers, timers_store

from .fake_codie import FakeCodie
from .test_tool_list import add_entity, put_in_area, setup_assist
from .test_tools import INTEGRATION_ID, make_entry, setup, wait_connected


@pytest.fixture
def codie(fake_codie: FakeCodie) -> FakeCodie:
    """A Codie whose Character speaks English, which the built-in agent knows."""
    fake_codie.ai_language = "en"
    return fake_codie


async def start_codie(hass: HomeAssistant, fake: FakeCodie) -> None:
    """Set up Assist with a kitchen lamp the built-in agent can switch, then the Codie."""
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen lamp", True)
    await setup(hass, fake)
    await wait_connected(hass, fake, True)


async def call(fake: FakeCodie, tool: str, **arguments: Any) -> dict[str, Any]:
    return await fake.invoke(INTEGRATION_ID, tool, arguments)


async def test_a_command_the_built_in_agent_does_not_recognize_fails(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)

    result = await call(codie, "timer_start", command="make me a sandwich", minutes=5)

    assert result["status"] == "failed"
    assert result["code"] == "invalid_arguments"
    assert "rephrase" in result["message"].lower()


@pytest.mark.parametrize("command", ["turn off the kichen lamp", "turn off the lamp in the attic"])
async def test_a_command_naming_no_entity_or_area_fails(
    hass: HomeAssistant, codie: FakeCodie, command: str
) -> None:
    await start_codie(hass, codie)

    result = await call(codie, "timer_start", command=command, minutes=5)

    assert result["status"] == "failed"
    assert result["code"] == "target_not_found"
    assert "get_home_state" in result["message"]


async def test_a_recognized_command_starts_a_timer(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)

    result = await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)

    assert result["status"] == "succeeded"
    assert result["data"]["success"] is True
    assert "turn off the kitchen lamp" in result["data"]["result"]


async def elapse(hass: HomeAssistant, **delay: float) -> None:
    """Let `delay` pass for every callback Home Assistant has scheduled."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(**delay))
    await hass.async_block_till_done()


async def test_a_timer_runs_its_command_when_it_ends(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)
    turned_off = async_mock_service(hass, "light", "turn_off")
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)

    await elapse(hass, minutes=19)
    assert not turned_off
    await elapse(hass, minutes=20)

    (lamp,) = turned_off
    assert lamp.data["entity_id"] == ["light.kitchen_lamp"]
    assert not codie.utterances


async def test_a_command_that_names_no_area_acts_on_the_codies_area(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    # "Turn off the lights in 20 minutes", said to a Codie in the Office.
    await setup_assist(hass)
    put_in_area(hass, add_entity(hass, "light", "desk lamp", True), "Office")
    put_in_area(hass, add_entity(hass, "light", "pendant", True), "Kitchen")
    entry = await setup(hass, codie)
    await wait_connected(hass, codie, True)
    devices = dr.async_get(hass)
    (device,) = dr.async_entries_for_config_entry(devices, entry.entry_id)
    office = ar.async_get(hass).async_get_or_create("Office")
    devices.async_update_device(device.id, area_id=office.id)
    turned_off = async_mock_service(hass, "light", "turn_off")

    result = await call(codie, "timer_start", command="turn off the lights", minutes=20)
    await elapse(hass, minutes=21)

    assert result["status"] == "succeeded"
    assert sorted(e for c in turned_off for e in c.data["entity_id"]) == ["light.desk_lamp"]


async def test_a_command_that_fails_is_announced(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)
    # The Expose list is checked when the Command runs, so the lamp is gone by then.
    async_expose_entity(hass, "conversation", "light.kitchen_lamp", False)

    await elapse(hass, minutes=21)

    (utterance,) = codie.utterances
    # The agent's own speech, which is in the AI language.
    assert utterance["text"]
    assert not utterance["text"].startswith("I couldn't")


def agent_answer(
    speech: str | None = None,
    *,
    failed: tuple[intent.IntentResponseTarget, ...] = (),
) -> ConversationResult:
    """What the built-in agent answers with when a Command ends."""
    response = intent.IntentResponse(language="en")
    response.async_set_results(success_results=[], failed_results=list(failed))
    if speech is not None:
        response.async_set_speech(speech)
    return ConversationResult(response)


LAMP = intent.IntentResponseTarget(
    type=intent.IntentResponseTargetType.ENTITY, name="kitchen lamp", id="light.kitchen_lamp"
)


async def test_a_command_with_failed_targets_is_announced(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)

    with patch(
        "custom_components.codie.timers.async_converse",
        return_value=agent_answer("La lampe n'a pas répondu", failed=(LAMP,)),
    ):
        await elapse(hass, minutes=21)

    (utterance,) = codie.utterances
    assert utterance["text"] == "La lampe n'a pas répondu"


async def test_a_failure_without_speech_is_announced_in_english(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)

    with patch(
        "custom_components.codie.timers.async_converse", return_value=agent_answer(failed=(LAMP,))
    ):
        await elapse(hass, minutes=21)

    (utterance,) = codie.utterances
    assert utterance["text"] == (
        "I couldn't turn off the kitchen lamp: Home Assistant could not do it"
    )


async def test_a_command_that_succeeds_is_not_announced(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)

    with patch(
        "custom_components.codie.timers.async_converse", return_value=agent_answer("Turned off")
    ):
        await elapse(hass, minutes=21)

    assert not codie.utterances


async def test_a_command_that_raises_is_announced(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)

    with patch(
        "custom_components.codie.timers.async_converse", side_effect=HomeAssistantError("boom")
    ):
        await elapse(hass, minutes=21)

    (utterance,) = codie.utterances
    assert utterance["text"] == "I couldn't turn off the kitchen lamp: boom"


async def test_a_failure_is_logged_when_the_codie_cannot_say_it(
    hass: HomeAssistant, codie: FakeCodie, caplog: pytest.LogCaptureFixture
) -> None:
    await start_codie(hass, codie)
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=20)
    async_expose_entity(hass, "conversation", "light.kitchen_lamp", False)
    codie.utterance_error = (409, "DEVICE_BLOCKED")

    with caplog.at_level(logging.WARNING):
        await elapse(hass, minutes=21)

    assert "turn off the kitchen lamp" in caplog.text
    assert "could not say so" in caplog.text


@pytest.mark.parametrize(
    ("duration", "complaint"),
    [
        ({}, "longer than zero"),
        ({"minutes": 0, "seconds": 0}, "longer than zero"),
        ({"hours": 24, "seconds": 1}, "24 hours"),
        ({"minutes": 1441}, "24 hours"),
        ({"minutes": -5}, "whole number"),
        ({"hours": 1.5}, "whole number"),
    ],
)
async def test_a_duration_outside_the_limits_is_refused(
    hass: HomeAssistant, codie: FakeCodie, duration: dict[str, Any], complaint: str
) -> None:
    await start_codie(hass, codie)

    result = await call(codie, "timer_start", command="turn off the kitchen lamp", **duration)

    assert result["status"] == "failed"
    assert result["code"] == "invalid_arguments"
    assert complaint in result["message"]


async def test_a_timer_may_last_exactly_24_hours(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)

    result = await call(codie, "timer_start", command="turn off the kitchen lamp", hours=24)

    assert result["status"] == "succeeded"


async def test_at_most_10_timers_run_at_once(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)
    for number in range(10):
        started = await call(
            codie, "timer_start", command="turn off the kitchen lamp", minutes=5, name=f"t{number}"
        )
        assert started["status"] == "succeeded"

    result = await call(
        codie, "timer_start", command="turn off the kitchen lamp", minutes=5, name="eleventh"
    )

    assert result["status"] == "failed"
    assert result["code"] == "invalid_arguments"
    assert "10" in result["message"]


@pytest.fixture
def slow_check() -> Generator[None]:
    """Make the Command check take a moment, as it does when the agent first loads its intents."""
    check = CodieTimers._async_check

    async def slow(self: CodieTimers, command: str) -> None:
        await asyncio.sleep(0.05)
        await check(self, command)

    with patch.object(CodieTimers, "_async_check", slow):
        yield


async def test_two_starts_at_nine_timers_let_only_one_through(
    hass: HomeAssistant, codie: FakeCodie, slow_check: None
) -> None:
    await start_codie(hass, codie)
    turned_off = async_mock_service(hass, "light", "turn_off")
    for number in range(9):
        await start_timer(codie, f"t{number}", minutes=5)

    results = await asyncio.gather(
        call(codie, "timer_start", command="turn off the kitchen lamp", minutes=5, name="a"),
        call(codie, "timer_start", command="turn off the kitchen lamp", minutes=5, name="b"),
    )

    assert sorted(r["status"] for r in results) == ["failed", "succeeded"]
    await elapse(hass, minutes=6)
    assert len(turned_off) == 10


async def test_two_starts_with_one_name_let_only_one_through(
    hass: HomeAssistant, codie: FakeCodie, slow_check: None
) -> None:
    await start_codie(hass, codie)
    turned_off = async_mock_service(hass, "light", "turn_off")

    results = await asyncio.gather(
        *(
            call(codie, "timer_start", command="turn off the kitchen lamp", minutes=5, name="Lamp")
            for _ in range(2)
        )
    )

    assert sorted(r["status"] for r in results) == ["failed", "succeeded"]
    await elapse(hass, minutes=6)
    assert len(turned_off) == 1  # no orphaned callback ran a second time


async def test_a_start_that_ends_after_unload_schedules_and_saves_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen lamp", True)
    turned_off = async_mock_service(hass, "light", "turn_off")
    store = timers_store(hass, "gone")
    timers = CodieTimers(hass, store, "en", None, AsyncMock())
    checking = asyncio.Event()
    release = asyncio.Event()

    async def slow_check(command: str) -> None:
        checking.set()
        await release.wait()

    with patch.object(timers, "_async_check", slow_check):
        start = asyncio.create_task(
            timers.async_start({"command": "turn off the kitchen lamp", "minutes": 5})
        )
        await checking.wait()
        timers.async_unload()
        release.set()
        with pytest.raises(ToolError) as raised:
            await start

    assert raised.value.code == "tool_unavailable"
    await elapse(hass, minutes=6)
    assert not turned_off
    assert store.key not in hass_storage


async def test_a_name_in_use_is_refused_whatever_its_case(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=5, name="Lamp")

    result = await call(
        codie, "timer_start", command="turn off the kitchen lamp", minutes=9, name="lamp"
    )

    assert result["status"] == "failed"
    assert result["code"] == "invalid_arguments"
    assert "already" in result["message"]


async def start_timer(
    fake: FakeCodie,
    name: str | None = None,
    command: str = "turn off the kitchen lamp",
    **delay: int,
) -> None:
    arguments: dict[str, Any] = {"command": command, **delay}
    if name is not None:
        arguments["name"] = name
    assert (await call(fake, "timer_start", **arguments))["status"] == "succeeded"


async def test_status_lists_every_timer_with_its_time_left_and_command(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await start_timer(codie, "Lamp", hours=1, minutes=5)
    await start_timer(codie, None, seconds=90)

    result = await call(codie, "timer_control", action="status")

    assert result["status"] == "succeeded"
    text = result["data"]["result"]
    assert "Lamp: 1 h 5 min left, runs: turn off the kitchen lamp" in text
    assert "turn off the kitchen lamp: 1 min 30 s left, runs: turn off the kitchen lamp" in text


async def test_status_without_timers_says_so(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)

    result = await call(codie, "timer_control", action="status")

    assert result["status"] == "succeeded"
    assert result["data"]["result"] == "No Timers are running"


async def test_status_by_name_ignores_case_and_counts_down(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await start_timer(codie, "Lamp", minutes=10)
    await start_timer(codie, "Heater", minutes=30)

    later = dt_util.utcnow() + timedelta(minutes=4)
    with patch("custom_components.codie.timers.dt_util.utcnow", return_value=later):
        result = await call(codie, "timer_control", action="status", name="lAmP")

    assert result["data"]["result"] == "Lamp: 6 min left, runs: turn off the kitchen lamp"


async def test_status_of_an_unknown_name_is_not_found(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await start_timer(codie, "Lamp", minutes=10)

    result = await call(codie, "timer_control", action="status", name="oven")

    assert result["status"] == "failed"
    assert result["code"] == "target_not_found"
    assert "Lamp" in result["message"]


async def test_cancel_without_a_name_stops_the_only_timer(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    turned_off = async_mock_service(hass, "light", "turn_off")
    await start_timer(codie, None, minutes=10)

    result = await call(codie, "timer_control", action="cancel")

    assert result["status"] == "succeeded"
    await elapse(hass, minutes=11)
    assert not turned_off
    status = await call(codie, "timer_control", action="status")
    assert status["data"]["result"] == "No Timers are running"


async def test_cancel_with_several_timers_asks_which(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)
    await start_timer(codie, "Lamp", minutes=10)
    await start_timer(codie, "Heater", minutes=30)

    result = await call(codie, "timer_control", action="cancel")

    assert result["status"] == "needs_clarification"
    assert result["code"] == "ambiguous_target"
    assert "Lamp" in result["message"]
    assert "Heater" in result["message"]


async def test_cancel_by_name_stops_only_that_timer(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)
    add_entity(hass, "light", "hall light", True)
    turned_off = async_mock_service(hass, "light", "turn_off")
    await start_timer(codie, "Lamp", minutes=10)
    await start_timer(codie, "Hall", command="turn off the hall light", minutes=10)

    result = await call(codie, "timer_control", action="cancel", name="lamp")

    assert result["status"] == "succeeded"
    await elapse(hass, minutes=11)
    (ran,) = turned_off
    assert ran.data["entity_id"] == ["light.hall_light"]  # only Hall ran


async def test_cancel_of_an_unknown_name_or_with_none_running_is_not_found(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)

    nothing = await call(codie, "timer_control", action="cancel")
    await start_timer(codie, "Lamp", minutes=10)
    unknown = await call(codie, "timer_control", action="cancel", name="oven")

    for result in (nothing, unknown):
        assert result["status"] == "failed"
        assert result["code"] == "target_not_found"


async def test_a_part_of_a_name_finds_the_only_timer_it_fits(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await start_timer(codie, "Kitchen lamp off", minutes=10)
    await start_timer(codie, "Heater off", minutes=10)

    result = await call(codie, "timer_control", action="cancel", name="kitchen")
    remaining = await call(codie, "timer_control", action="status", name="off")

    assert result["status"] == "succeeded"
    assert "Kitchen lamp off" in result["data"]["result"]
    assert remaining["status"] == "succeeded"  # Heater off is now the only one that fits
    assert remaining["data"]["result"].startswith("Heater off: ")


async def test_a_part_of_a_name_that_fits_two_timers_asks_which(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    await start_timer(codie, "Kitchen lamp off", minutes=10)
    await start_timer(codie, "Heater off", minutes=10)

    result = await call(codie, "timer_control", action="cancel", name="off")

    assert result["status"] == "needs_clarification"
    assert result["code"] == "ambiguous_target"
    assert "Kitchen lamp off" in result["message"]
    assert "Heater off" in result["message"]


async def test_a_control_action_must_be_cancel_or_status(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)

    result = await call(codie, "timer_control", action="pause")

    assert result["status"] == "failed"
    assert result["code"] == "invalid_arguments"


async def test_a_sentence_trigger_is_accepted_and_runs_when_the_timer_ends(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await start_codie(hass, codie)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": {"trigger": "conversation", "command": "start movie night"},
                "actions": {"action": "test.movie_night"},
            }
        },
    )
    started = async_mock_service(hass, "test", "movie_night")

    result = await call(codie, "timer_start", command="start movie night", minutes=5)
    await elapse(hass, minutes=6)

    assert result["status"] == "succeeded"
    assert len(started) == 1


def offered(fake: FakeCodie) -> set[str]:
    tools: list[dict[str, Any]] = fake.tool_integrations[INTEGRATION_ID]["catalog"]["tools"]
    return {tool["name"] for tool in tools if tool["name"].startswith("timer_")}


async def test_the_catalog_offers_both_timer_tools(hass: HomeAssistant, codie: FakeCodie) -> None:
    await start_codie(hass, codie)

    assert offered(codie) == {"timer_start", "timer_control"}


async def test_the_timer_tools_are_unavailable_in_a_language_the_agent_lacks(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    codie.ai_language = "xx"
    await start_codie(hass, codie)

    result = await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=5)

    assert offered(codie) == set()
    assert result["status"] == "failed"
    assert result["code"] == "tool_unavailable"


async def test_the_timer_tools_are_unavailable_without_a_language(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    async def system() -> dict[str, Any]:
        return {"language": "en"}

    with patch("custom_components.codie.api.CodieClient.async_get_system", side_effect=system):
        await start_codie(hass, codie)

    assert offered(codie) == set()


async def test_the_timer_tools_are_unavailable_without_the_recognizer(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    with patch.object(DefaultAgent, "async_recognize_sentence_trigger", None):
        await start_codie(hass, codie)
        result = await call(codie, "timer_start", command="turn off the kitchen lamp", minutes=5)

    assert offered(codie) == set()
    assert result["code"] == "tool_unavailable"


async def test_the_timer_tools_are_unavailable_without_the_built_in_agent(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await setup(hass, codie)  # conversation is not set up, so there is no built-in agent

    assert offered(codie) == set()


def stored_timers(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry: MockConfigEntry
) -> list[dict[str, Any]]:
    stored = hass_storage.get(timers_store(hass, entry.entry_id).key)
    timers: list[dict[str, Any]] = stored["data"]["timers"] if stored else []
    return timers


def store_timers(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entry: MockConfigEntry,
    *timers: dict[str, Any],
) -> None:
    """What a Home Assistant that stopped earlier left behind."""
    key = timers_store(hass, entry.entry_id).key
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"timers": list(timers)},
    }


async def test_timers_survive_a_reload_with_their_remaining_time(
    hass: HomeAssistant, codie: FakeCodie
) -> None:
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen lamp", True)
    turned_off = async_mock_service(hass, "light", "turn_off")
    entry = await setup(hass, codie)
    await wait_connected(hass, codie, True)
    await start_timer(codie, "Lamp", minutes=20)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await elapse(hass, minutes=10)  # no callback is left to run it
    assert not turned_off
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_connected(hass, codie, True)

    status = await call(codie, "timer_control", action="status")
    assert status["data"]["result"].startswith("Lamp: ")
    await elapse(hass, minutes=21)
    assert len(turned_off) == 1


async def resume_entry(
    hass: HomeAssistant, codie: FakeCodie, hass_storage: dict[str, Any], *timers: dict[str, Any]
) -> MockConfigEntry:
    """Set up a Codie whose Home Assistant stopped earlier with `timers` stored."""
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen lamp", True)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=codie.machine_id, data=make_entry(codie).data, title="Codie"
    )
    entry.add_to_hass(hass)
    store_timers(hass, hass_storage, entry, *timers)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_connected(hass, codie, True)
    return entry


async def test_stored_timers_resume_with_the_time_they_have_left(
    hass: HomeAssistant, codie: FakeCodie, hass_storage: dict[str, Any]
) -> None:
    await resume_entry(
        hass,
        codie,
        hass_storage,
        {
            "name": "Lamp",
            "command": "turn off the kitchen lamp",
            "ends_at": dt_util.utcnow().timestamp() + 450,
        },
    )
    turned_off = async_mock_service(hass, "light", "turn_off")

    status = await call(codie, "timer_control", action="status")
    await elapse(hass, minutes=11)

    assert status["data"]["result"].startswith("Lamp: 7 min 30 s left")
    assert len(turned_off) == 1


async def test_a_timer_that_ended_while_home_assistant_was_down_is_dropped(
    hass: HomeAssistant,
    codie: FakeCodie,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = dt_util.utcnow().timestamp()

    with caplog.at_level(logging.WARNING):
        entry = await resume_entry(
            hass,
            codie,
            hass_storage,
            {"name": "Late", "command": "turn off the kitchen lamp", "ends_at": now - 60},
            {"name": "Soon", "command": "turn off the kitchen lamp", "ends_at": now + 60},
        )
    turned_off = async_mock_service(hass, "light", "turn_off")
    await elapse(hass, minutes=2)

    assert "Dropping the Timer 'Late'" in caplog.text
    assert len(turned_off) == 1  # only Soon ran, and Late never did
    assert stored_timers(hass, hass_storage, entry) == []
    assert not codie.utterances


async def test_a_stored_timer_that_cannot_be_read_is_dropped(
    hass: HomeAssistant,
    codie: FakeCodie,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        await resume_entry(hass, codie, hass_storage, {"name": "Broken"})

    status = await call(codie, "timer_control", action="status")
    assert status["data"]["result"] == "No Timers are running"
    assert "Dropping a stored Timer" in caplog.text


async def test_the_store_follows_the_timers(
    hass: HomeAssistant, codie: FakeCodie, hass_storage: dict[str, Any]
) -> None:
    entry = await resume_entry(hass, codie, hass_storage)
    async_mock_service(hass, "light", "turn_off")

    await start_timer(codie, "Lamp", minutes=10)
    await start_timer(codie, "Heater", minutes=20)
    assert [t["name"] for t in stored_timers(hass, hass_storage, entry)] == ["Lamp", "Heater"]
    await call(codie, "timer_control", action="cancel", name="Heater")
    assert [t["name"] for t in stored_timers(hass, hass_storage, entry)] == ["Lamp"]
    await elapse(hass, minutes=11)

    assert stored_timers(hass, hass_storage, entry) == []


async def test_removing_the_codie_deletes_its_timers(
    hass: HomeAssistant, codie: FakeCodie, hass_storage: dict[str, Any]
) -> None:
    entry = await resume_entry(hass, codie, hass_storage)
    turned_off = async_mock_service(hass, "light", "turn_off")
    await start_timer(codie, "Lamp", minutes=10)

    assert await hass.config_entries.async_remove(entry.entry_id)
    await elapse(hass, minutes=11)

    assert timers_store(hass, entry.entry_id).key not in hass_storage
    assert not turned_off
