"""Voice volume, Music volume, the screen and the side lights, against the fake Codie."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from .fake_codie import FakeCodie, RecordedRequest
from .test_reauth import reauth_flows
from .test_tools import INTEGRATION_ID, make_entry, setup

VOICE_VOLUME = "number.codie_voice_volume"
MUSIC_VOLUME = "number.codie_music_volume"
BRIGHTNESS = "number.codie_screen_brightness"
SCREEN = "switch.codie_screen"
SIDE_LIGHTS = "switch.codie_side_lights"
MUSIC_SIDE_LIGHTS = "switch.codie_music_side_lights"


@pytest.fixture(autouse=True)
def fast_music_confirmation() -> Generator[None]:
    with (
        patch("custom_components.codie.settings.MUSIC_CONFIRM_INTERVAL_S", 0.01),
        patch("custom_components.codie.settings.MUSIC_CONFIRM_TIMEOUT_S", 0.2),
    ):
        yield


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})


async def set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def switch(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True
    )


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.settings.async_refresh()
    await hass.async_block_till_done()


def writes(fake: FakeCodie) -> list[RecordedRequest]:
    return [
        r
        for r in fake.signed_requests()
        if r.method in ("PUT", "POST") and "tool-integrations" not in r.path
    ]


async def test_each_value_is_read(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    assert state_of(hass, VOICE_VOLUME) == "30"
    assert state_of(hass, MUSIC_VOLUME) == "55"
    assert state_of(hass, BRIGHTNESS) == "70"
    assert state_of(hass, SCREEN) == STATE_ON
    assert state_of(hass, SIDE_LIGHTS) == STATE_ON
    assert state_of(hass, MUSIC_SIDE_LIGHTS) == STATE_OFF


async def test_the_entities_belong_to_the_codie_device_with_the_right_categories(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    registry = er.async_get(hass)
    expected = {
        VOICE_VOLUME: ("voice_volume", None),
        MUSIC_VOLUME: ("music_volume", None),
        BRIGHTNESS: ("screen_brightness", EntityCategory.CONFIG),
        SCREEN: ("screen", None),
        SIDE_LIGHTS: ("side_lights", EntityCategory.CONFIG),
        MUSIC_SIDE_LIGHTS: ("music_side_lights", EntityCategory.CONFIG),
    }
    devices = {registry.async_get(entity_id).device_id for entity_id in expected}  # type: ignore[union-attr]
    assert len(devices) == 1
    for entity_id, (key, category) in expected.items():
        entity = registry.async_get(entity_id)
        assert entity is not None
        assert entity.unique_id == f"{fake_codie.machine_id}_{key}"
        assert entity.entity_category == category


async def test_voice_volume_follows_the_active_slot(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    assert state_of(hass, VOICE_VOLUME) == "30"
    # It comes with the Character list, so the Character is not read on its own.
    assert [r for r in fake_codie.requests_to("/data/user/character") if r.method == "GET"] == []

    fake_codie.slot = 5
    await refresh(hass, entry)

    assert state_of(hass, VOICE_VOLUME) == "50"


async def test_setting_the_voice_volume_leaves_the_slot_to_the_codie(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    await set_number(hass, VOICE_VOLUME, 80)

    (write,) = writes(fake_codie)
    assert (write.method, write.path) == ("PUT", "/v1/api/data/user/character")
    assert write.json == {"tts_volume": 80}
    assert fake_codie.voice_volumes[3] == 80
    assert state_of(hass, VOICE_VOLUME) == "80"


async def test_setting_the_music_volume(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    await set_number(hass, MUSIC_VOLUME, 45)

    (write,) = writes(fake_codie)
    assert (write.method, write.path) == ("POST", "/v1/api/data/user/music/control")
    body = write.json
    assert set(body) == {"action", "volume_level", "request_id"}
    assert body["action"] == "SET_VOLUME"
    # The Codie rounds down to its steps, so the lowest level of the step is sent.
    assert body["volume_level"] == 46
    assert str(uuid.UUID(body["request_id"])) == body["request_id"]
    assert state_of(hass, MUSIC_VOLUME) == "45"


async def test_setting_the_brightness(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    await set_number(hass, BRIGHTNESS, 35)

    (write,) = writes(fake_codie)
    assert (write.method, write.path) == ("PUT", "/v1/api/data/user/device/brightness")
    assert write.json == {"level": 35}
    assert state_of(hass, BRIGHTNESS) == "35"


@pytest.mark.parametrize("on", [False, True])
async def test_switching_the_screen(hass: HomeAssistant, fake_codie: FakeCodie, on: bool) -> None:
    await setup(hass, fake_codie)
    fake_codie.screen_on = not on

    await switch(hass, SCREEN, on)

    (write,) = writes(fake_codie)
    assert (write.method, write.path) == ("PUT", "/v1/api/data/user/device/screen")
    assert write.json == {"on": on}
    assert state_of(hass, SCREEN) == (STATE_ON if on else STATE_OFF)


@pytest.mark.parametrize(
    ("entity_id", "field", "initial"),
    [
        (SIDE_LIGHTS, "normalEnabled", True),
        (MUSIC_SIDE_LIGHTS, "musicEnabled", False),
    ],
)
async def test_a_side_light_switch_sends_only_its_own_field(
    hass: HomeAssistant, fake_codie: FakeCodie, entity_id: str, field: str, initial: bool
) -> None:
    await setup(hass, fake_codie)
    other = {SIDE_LIGHTS: MUSIC_SIDE_LIGHTS, MUSIC_SIDE_LIGHTS: SIDE_LIGHTS}[entity_id]
    other_before = state_of(hass, other)

    await switch(hass, entity_id, not initial)

    (write,) = writes(fake_codie)
    assert (write.method, write.path) == ("POST", "/v1/api/data/user/light-master-switch")
    assert write.json == {field: not initial}
    assert state_of(hass, entity_id) == (STATE_OFF if initial else STATE_ON)
    assert state_of(hass, other) == other_before


async def test_every_write_is_read_back(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)
    fake_codie.requests.clear()

    await switch(hass, SIDE_LIGHTS, False)

    methods = [(r.method, r.path.removeprefix("/v1/api")) for r in fake_codie.signed_requests()]
    assert methods[0] == ("POST", "/data/user/light-master-switch")
    assert ("GET", "/data/user/light-master-switch") in methods[1:]


async def test_the_settings_are_polled_every_30_seconds(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.music_volume = 12

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    # The poll talks to a real socket, so block_till_done alone may return before it ends.
    for _ in range(100):
        await hass.async_block_till_done()
        if state_of(hass, MUSIC_VOLUME) == "12":
            break
        await asyncio.sleep(0.05)

    assert state_of(hass, MUSIC_VOLUME) == "12"


async def test_the_screen_is_unknown_when_the_codie_cannot_say(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    fake_codie.screen_on = None
    await refresh(hass, entry)

    assert state_of(hass, SCREEN) == STATE_UNKNOWN


@pytest.mark.parametrize(("level", "writable"), [(70, False), (None, True), (None, False)], ids=str)
async def test_the_brightness_is_unavailable_when_it_cannot_be_set(
    hass: HomeAssistant, fake_codie: FakeCodie, level: int | None, writable: bool
) -> None:
    entry = await setup(hass, fake_codie)
    assert state_of(hass, BRIGHTNESS) == "70"

    fake_codie.brightness = level
    fake_codie.brightness_writable = writable
    await refresh(hass, entry)

    assert state_of(hass, BRIGHTNESS) == STATE_UNAVAILABLE
    # Nothing else is affected.
    assert state_of(hass, MUSIC_VOLUME) == "55"


@pytest.mark.parametrize(
    ("entity_id", "service_data"),
    [
        (VOICE_VOLUME, 10),
        (MUSIC_VOLUME, 10),
        (BRIGHTNESS, 10),
        (SCREEN, True),
        (SIDE_LIGHTS, False),
        (MUSIC_SIDE_LIGHTS, True),
    ],
)
async def test_a_refused_write_raises_and_reads_the_value_again(
    hass: HomeAssistant, fake_codie: FakeCodie, entity_id: str, service_data: Any
) -> None:
    entry = await setup(hass, fake_codie)
    before = state_of(hass, entity_id)
    fake_codie.setting_write_error = (400, "INVALID_REQUEST")
    fake_codie.requests.clear()

    with pytest.raises(HomeAssistantError, match="The Codie refused the change"):
        if isinstance(service_data, bool):
            await switch(hass, entity_id, service_data)
        else:
            await set_number(hass, entity_id, service_data)

    gets = [r for r in fake_codie.signed_requests() if r.method == "GET"]
    assert gets, "the settings were not read again"
    assert state_of(hass, entity_id) == before
    assert entry.state is ConfigEntryState.LOADED
    assert reauth_flows(hass, entry) == []


@pytest.mark.parametrize(
    ("status", "code"), [(409, "SCREEN_NOT_CONFIRMED"), (503, "SCREEN_READBACK_FAILED")]
)
async def test_a_screen_command_that_was_not_confirmed_is_a_failed_write(
    hass: HomeAssistant, fake_codie: FakeCodie, status: int, code: str
) -> None:
    await setup(hass, fake_codie)
    fake_codie.screen_unconfirmed = (status, code)

    with pytest.raises(HomeAssistantError, match="could not confirm it"):
        await switch(hass, SCREEN, False)

    # The Codie did send it, and the read-back shows the screen changed.
    assert state_of(hass, SCREEN) == STATE_OFF


@pytest.mark.parametrize(
    ("entity_id", "status", "code"),
    [
        (BRIGHTNESS, 409, "CONFLICT"),
        (BRIGHTNESS, 503, "SCREEN_READBACK_FAILED"),
        (SCREEN, 503, "SERVICE_NOT_READY"),
        (SIDE_LIGHTS, 409, "CONFLICT"),
        (MUSIC_VOLUME, 503, "SERVICE_NOT_READY"),
    ],
)
async def test_only_a_screen_command_can_be_unconfirmed(
    hass: HomeAssistant, fake_codie: FakeCodie, entity_id: str, status: int, code: str
) -> None:
    await setup(hass, fake_codie)
    fake_codie.setting_write_error = (status, code)

    with pytest.raises(HomeAssistantError, match="The Codie refused the change") as raised:
        if entity_id.startswith("number."):
            await set_number(hass, entity_id, 10)
        else:
            await switch(hass, entity_id, False)

    assert "could not confirm" not in str(raised.value)


async def test_a_write_the_codie_cannot_be_reached_for_fails(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    await fake_codie.stop()

    with pytest.raises(HomeAssistantError, match="Could not reach the Codie"):
        await set_number(hass, MUSIC_VOLUME, 10)


async def test_a_write_the_codie_rejects_the_credentials_for_starts_reauth(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.auth_error = "LOCAL_SECRET_MISMATCH"

    with pytest.raises(HomeAssistantError, match="no longer accepts Home Assistant"):
        await switch(hass, SIDE_LIGHTS, False)
    await hass.async_block_till_done()

    assert len(reauth_flows(hass, entry)) == 1


async def test_the_entities_are_unavailable_when_the_codie_cannot_be_reached(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    await fake_codie.stop()

    await refresh(hass, entry)

    for entity_id in (
        VOICE_VOLUME,
        MUSIC_VOLUME,
        BRIGHTNESS,
        SCREEN,
        SIDE_LIGHTS,
        MUSIC_SIDE_LIGHTS,
    ):
        assert state_of(hass, entity_id) == STATE_UNAVAILABLE
    # A poll that fails is not an authentication problem.
    assert reauth_flows(hass, entry) == []


async def test_a_setting_that_cannot_be_read_leaves_the_others_available(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.overrides["/data/user/device/brightness"] = (500, b"{}")
    await refresh(hass, entry)

    assert state_of(hass, BRIGHTNESS) == STATE_UNAVAILABLE
    assert state_of(hass, VOICE_VOLUME) == "30"
    assert state_of(hass, MUSIC_VOLUME) == "55"
    assert state_of(hass, SCREEN) == STATE_ON
    assert state_of(hass, SIDE_LIGHTS) == STATE_ON

    del fake_codie.overrides["/data/user/device/brightness"]
    await refresh(hass, entry)

    assert state_of(hass, BRIGHTNESS) == "70"


SETTING_PATHS = [
    "/data/user/slot",
    "/data/user/music/playback",
    "/data/user/music/page",
    "/data/user/device/brightness",
    "/data/user/device/screen",
    "/data/user/light-master-switch",
]


async def test_the_entities_are_unavailable_when_every_setting_fails_to_read(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    for path in SETTING_PATHS:
        fake_codie.overrides[path] = (500, b"{}")
    await refresh(hass, entry)

    for entity_id in (
        VOICE_VOLUME,
        MUSIC_VOLUME,
        BRIGHTNESS,
        SCREEN,
        SIDE_LIGHTS,
        MUSIC_SIDE_LIGHTS,
    ):
        assert state_of(hass, entity_id) == STATE_UNAVAILABLE

    fake_codie.overrides.clear()
    await refresh(hass, entry)

    assert state_of(hass, SIDE_LIGHTS) == STATE_ON


async def test_the_music_volume_is_unavailable_unless_the_player_is_ready(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    fake_codie.music_volume_max = 50
    await refresh(hass, entry)

    assert state_of(hass, MUSIC_VOLUME) == STATE_UNAVAILABLE
    assert state_of(hass, VOICE_VOLUME) == "30"

    fake_codie.music_volume_max = 100
    await refresh(hass, entry)

    assert state_of(hass, MUSIC_VOLUME) == "55"


async def test_a_music_volume_the_codie_leaves_out_is_zero(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    fake_codie.music_volume = 0
    await refresh(hass, entry)

    assert state_of(hass, MUSIC_VOLUME) == "0"


async def test_the_music_volume_waits_for_the_codie_to_apply_it(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.requests.clear()
    fake_codie.music_apply_after_reads = 3

    await set_number(hass, MUSIC_VOLUME, 18)

    # One before the command, three while waiting, then the read-back.
    assert len(fake_codie.requests_to("/data/user/music/playback")) == 5
    assert state_of(hass, MUSIC_VOLUME) == "18"


async def test_a_music_volume_the_codie_reports_an_error_for_fails(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.music_error_code = "PLAYER_BUSY"

    with pytest.raises(HomeAssistantError, match="Music volume: PLAYER_BUSY"):
        await set_number(hass, MUSIC_VOLUME, 20)

    assert state_of(hass, MUSIC_VOLUME) == "55"


@pytest.mark.parametrize(("volume", "shown"), [(40, "36"), (41, "45"), (100, "100"), (3, "0")])
async def test_a_music_volume_between_steps_goes_to_the_nearest_step(
    hass: HomeAssistant, fake_codie: FakeCodie, volume: int, shown: str
) -> None:
    await setup(hass, fake_codie)

    await set_number(hass, MUSIC_VOLUME, volume)

    assert state_of(hass, MUSIC_VOLUME) == shown


@pytest.mark.parametrize("volume", [9, 18, 27, 36, 45, 54, 63, 72, 81, 90])
async def test_setting_a_music_volume_the_codie_reported_keeps_it(
    hass: HomeAssistant, fake_codie: FakeCodie, volume: int
) -> None:
    await setup(hass, fake_codie)

    await set_number(hass, MUSIC_VOLUME, volume)

    assert state_of(hass, MUSIC_VOLUME) == str(volume)


async def test_setting_the_music_volume_to_zero(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    await set_number(hass, MUSIC_VOLUME, 0)

    assert fake_codie.music_volume == 0
    assert state_of(hass, MUSIC_VOLUME) == "0"


async def test_two_quick_music_volume_writes_do_not_fail(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    await asyncio.gather(set_number(hass, MUSIC_VOLUME, 18), set_number(hass, MUSIC_VOLUME, 27))

    # Whichever command reached the Codie last is the one it ran.
    assert state_of(hass, MUSIC_VOLUME) in ("18", "27")


async def test_a_music_volume_the_codie_does_not_apply_in_time_fails(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.music_apply_after_reads = None

    with pytest.raises(
        HomeAssistantError, match="could not confirm it: the Music volume is 55, not 20"
    ):
        await set_number(hass, MUSIC_VOLUME, 20)

    assert state_of(hass, MUSIC_VOLUME) == "55"
    # The Codie gets to it later, and the next poll shows it.
    fake_codie.music_volume = 20
    await refresh(hass, entry)
    assert state_of(hass, MUSIC_VOLUME) == "20"


async def test_a_music_volume_that_is_already_right_when_the_wait_ends_succeeds(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    # The Codie never says it applied the command, but the volume is what was asked for.
    fake_codie.music_apply_after_reads = None
    fake_codie.music_volume = 20

    await set_number(hass, MUSIC_VOLUME, 20)

    assert state_of(hass, MUSIC_VOLUME) == "20"


async def test_a_poll_the_codie_rejects_the_credentials_for_starts_reauth(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    fake_codie.auth_error = "LOCAL_SECRET_MISMATCH"

    await refresh(hass, entry)

    assert state_of(hass, SIDE_LIGHTS) == STATE_UNAVAILABLE
    assert len(reauth_flows(hass, entry)) == 1


async def test_setup_does_not_wait_for_the_settings(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    for path in SETTING_PATHS:
        fake_codie.overrides[path] = (500, b"{}")
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    for entity_id in (
        VOICE_VOLUME,
        MUSIC_VOLUME,
        BRIGHTNESS,
        SCREEN,
        SIDE_LIGHTS,
        MUSIC_SIDE_LIGHTS,
    ):
        assert state_of(hass, entity_id) == STATE_UNAVAILABLE
    assert INTEGRATION_ID in fake_codie.tool_integrations

    fake_codie.overrides.clear()
    await refresh(hass, entry)

    assert state_of(hass, SIDE_LIGHTS) == STATE_ON
