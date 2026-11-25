"""The Active Character sensor and the Character select, against the fake Codie."""

from __future__ import annotations

import json

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .fake_codie import FakeCodie
from .test_settings import VOICE_VOLUME, refresh, state_of, writes
from .test_tools import setup

ACTIVE_CHARACTER = "sensor.codie_active_character"
CHARACTER = "select.codie_character"


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})


async def select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        "select", "select_option", {"entity_id": CHARACTER, "option": option}, blocking=True
    )


def options(hass: HomeAssistant) -> list[str]:
    state = hass.states.get(CHARACTER)
    assert state is not None
    return list(state.attributes["options"])


async def test_the_sensor_shows_the_character_in_the_active_slot(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    state = hass.states.get(ACTIVE_CHARACTER)
    assert state is not None
    assert state.state == "Cleo"
    assert state.attributes["slot"] == 3

    # Switched on the Codie itself: the next poll shows it.
    fake_codie.slot = 1
    await refresh(hass, entry)

    state = hass.states.get(ACTIVE_CHARACTER)
    assert state is not None
    assert state.state == "Aiko"
    assert state.attributes["slot"] == 1


async def test_the_entities_belong_to_the_codie_device(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    registry = er.async_get(hass)
    sensor = registry.async_get(ACTIVE_CHARACTER)
    select_entry = registry.async_get(CHARACTER)
    voice = registry.async_get(VOICE_VOLUME)
    assert sensor is not None and select_entry is not None and voice is not None
    assert sensor.unique_id == f"{fake_codie.machine_id}_active_character"
    assert select_entry.unique_id == f"{fake_codie.machine_id}_character"
    assert sensor.device_id == select_entry.device_id == voice.device_id


async def test_the_select_has_one_option_per_slot(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    assert options(hass) == ["Aiko", "Bram", "Cleo", "Dax", "Esme"]
    assert state_of(hass, CHARACTER) == "Cleo"


async def test_selecting_a_character_switches_the_slot_and_the_voice_volume_follows(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    assert state_of(hass, VOICE_VOLUME) == "30"

    await select(hass, "Esme")

    (write,) = writes(fake_codie)
    assert (write.method, write.path) == ("PUT", "/v1/api/data/user/slot")
    assert write.json == {"slot_number": 5}
    assert fake_codie.slot == 5
    assert state_of(hass, CHARACTER) == "Esme"
    assert state_of(hass, ACTIVE_CHARACTER) == "Esme"
    assert state_of(hass, VOICE_VOLUME) == "50"


async def test_characters_with_the_same_name_or_none_are_told_apart(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.character_names = {1: "Aiko", 2: "Aiko", 3: "Cleo", 4: ""}
    fake_codie.slot = 2
    await setup(hass, fake_codie)

    assert options(hass) == ["Aiko (Slot 1)", "Aiko (Slot 2)", "Cleo", "Slot 4"]
    assert state_of(hass, CHARACTER) == "Aiko (Slot 2)"
    assert state_of(hass, ACTIVE_CHARACTER) == "Aiko"

    await select(hass, "Slot 4")

    assert fake_codie.slot == 4
    assert state_of(hass, CHARACTER) == "Slot 4"
    # The Character has no name to show.
    assert state_of(hass, ACTIVE_CHARACTER) == STATE_UNKNOWN


async def test_a_name_only_in_the_presentation_is_used(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/data/user/character/list"] = (
        200,
        json.dumps(
            {
                "success": True,
                "data": [
                    {"slot_number": 1, "name": "Aiko"},
                    {"slot_number": 3, "name": "", "presentation": {"name": "Nova"}},
                    {"name": "No slot"},
                ],
            }
        ).encode(),
    )
    await setup(hass, fake_codie)

    assert options(hass) == ["Aiko", "Nova"]
    assert state_of(hass, ACTIVE_CHARACTER) == "Nova"
    # The entry has no `tts_volume`, so the Voice volume is not known.
    assert state_of(hass, VOICE_VOLUME) == STATE_UNKNOWN


async def test_a_slot_missing_from_the_list_has_no_voice_volume(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    del fake_codie.character_names[3]
    await setup(hass, fake_codie)

    assert state_of(hass, VOICE_VOLUME) == STATE_UNAVAILABLE
    assert state_of(hass, CHARACTER) == STATE_UNKNOWN
    assert state_of(hass, ACTIVE_CHARACTER) == STATE_UNKNOWN


async def test_an_option_that_went_away_is_refused(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    entity = hass.data["entity_components"]["select"].get_entity(CHARACTER)

    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("Zed")

    assert writes(fake_codie) == []


async def test_a_refused_switch_raises_and_reads_the_slot_again(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.setting_write_error = (400, "INVALID_REQUEST")
    fake_codie.requests.clear()

    with pytest.raises(HomeAssistantError, match="The Codie refused the change"):
        await select(hass, "Aiko")

    assert fake_codie.requests_to("/data/user/slot")[-1].method == "GET"
    assert state_of(hass, CHARACTER) == "Cleo"


async def test_without_the_character_list_the_voice_volume_is_unavailable_too(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    fake_codie.overrides["/data/user/character/list"] = (500, b"{}")
    await refresh(hass, entry)

    assert state_of(hass, ACTIVE_CHARACTER) == STATE_UNAVAILABLE
    assert state_of(hass, CHARACTER) == STATE_UNAVAILABLE
    assert state_of(hass, VOICE_VOLUME) == STATE_UNAVAILABLE
    assert state_of(hass, "switch.codie_side_lights") == "on"

    fake_codie.character_names = {}
    del fake_codie.overrides["/data/user/character/list"]
    await refresh(hass, entry)

    # An empty list has nothing to show either.
    assert state_of(hass, CHARACTER) == STATE_UNAVAILABLE


async def test_without_the_slot_the_voice_volume_is_unavailable_too(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    fake_codie.overrides["/data/user/slot"] = (500, b"{}")
    await refresh(hass, entry)

    assert state_of(hass, ACTIVE_CHARACTER) == STATE_UNAVAILABLE
    assert state_of(hass, CHARACTER) == STATE_UNAVAILABLE
    assert state_of(hass, VOICE_VOLUME) == STATE_UNAVAILABLE
    assert state_of(hass, "switch.codie_side_lights") == "on"

    del fake_codie.overrides["/data/user/slot"]
    await refresh(hass, entry)

    assert state_of(hass, ACTIVE_CHARACTER) == "Cleo"
