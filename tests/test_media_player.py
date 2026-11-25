"""The Music media player, against the fake Codie."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.media_player import (
    ATTR_MEDIA_ALBUM_NAME,
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_POSITION_UPDATED_AT,
    ATTR_MEDIA_TITLE,
    ATTR_MEDIA_VOLUME_LEVEL,
    MediaPlayerEntityFeature,
    MediaType,
)
from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry

from .fake_codie import FakeCodie
from .test_settings import MUSIC_VOLUME, refresh, state_of, writes
from .test_tools import setup

MUSIC = "media_player.codie_music"


@pytest.fixture(autouse=True)
def fast_music_confirmation() -> Generator[None]:
    with (
        patch("custom_components.codie.settings.MUSIC_CONFIRM_INTERVAL_S", 0.01),
        patch("custom_components.codie.settings.MUSIC_CONFIRM_TIMEOUT_S", 0.2),
    ):
        yield


def music(hass: HomeAssistant) -> State:
    state = hass.states.get(MUSIC)
    assert state is not None
    return state


async def call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(
        "media_player", service, {"entity_id": MUSIC, **data}, blocking=True
    )


def actions(fake: FakeCodie) -> list[str]:
    return [r.json["action"] for r in writes(fake) if r.path.endswith("/music/control")]


async def test_the_music_player_belongs_to_the_codie_device(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    registry = er.async_get(hass)
    player = registry.async_get(MUSIC)
    volume = registry.async_get(MUSIC_VOLUME)
    assert player is not None
    assert volume is not None
    assert player.unique_id == f"{fake_codie.machine_id}_music"
    assert player.device_id == volume.device_id


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("PLAYING", "playing"),
        ("PAUSED", "paused"),
        ("LOADING", "buffering"),
        ("IDLE", "idle"),
        ("COMPLETED", "idle"),
        ("FAILED", "idle"),
        # The Codie leaves out an empty status.
        ("", "idle"),
    ],
)
async def test_the_state_follows_the_playback_status(
    hass: HomeAssistant, fake_codie: FakeCodie, status: str, state: str
) -> None:
    fake_codie.music_status = status
    await setup(hass, fake_codie)

    assert music(hass).state == state


async def test_the_status_changes_on_the_next_poll(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    assert music(hass).state == "paused"

    fake_codie.music_status = "PLAYING"
    await refresh(hass, entry)

    assert music(hass).state == "playing"


async def test_now_playing_shows_the_track(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    attributes = music(hass).attributes
    assert attributes[ATTR_MEDIA_TITLE] == "Morning Light"
    assert attributes[ATTR_MEDIA_ARTIST] == "The Codies"
    assert attributes[ATTR_MEDIA_ALBUM_NAME] == "Desk Songs"
    assert attributes[ATTR_MEDIA_CONTENT_TYPE] == MediaType.MUSIC
    assert attributes[ATTR_MEDIA_DURATION] == 200
    assert attributes[ATTR_MEDIA_POSITION] == 30
    assert attributes[ATTR_MEDIA_POSITION_UPDATED_AT] is not None


async def test_the_position_keeps_its_time_until_it_moves(
    hass: HomeAssistant, fake_codie: FakeCodie, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup(hass, fake_codie)
    paused_at = music(hass).attributes[ATTR_MEDIA_POSITION_UPDATED_AT]

    freezer.tick(30)
    await refresh(hass, entry)
    assert music(hass).attributes[ATTR_MEDIA_POSITION_UPDATED_AT] == paused_at

    freezer.tick(30)
    fake_codie.music_position_ms = 45_000
    await refresh(hass, entry)

    assert music(hass).attributes[ATTR_MEDIA_POSITION] == 45
    assert music(hass).attributes[ATTR_MEDIA_POSITION_UPDATED_AT] == dt_util.utcnow()


async def test_without_a_track_nothing_is_shown_as_playing(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.music_status = "IDLE"
    fake_codie.music_entry = None
    await setup(hass, fake_codie)

    attributes = music(hass).attributes
    for attribute in (
        ATTR_MEDIA_TITLE,
        ATTR_MEDIA_ARTIST,
        ATTR_MEDIA_ALBUM_NAME,
        ATTR_MEDIA_DURATION,
        ATTR_MEDIA_POSITION,
    ):
        assert attribute not in attributes


async def test_music_mode_is_an_attribute(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    entry = await setup(hass, fake_codie)
    assert music(hass).attributes["music_mode"] is True

    fake_codie.music_page_open = False
    await refresh(hass, entry)

    assert music(hass).attributes["music_mode"] is False


async def test_music_mode_is_left_out_when_the_codie_cannot_say(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/data/user/music/page"] = (500, b"{}")
    await setup(hass, fake_codie)

    assert music(hass).state == "paused"
    assert "music_mode" not in music(hass).attributes


async def test_the_volume_is_the_music_volume(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    assert music(hass).attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.55


async def test_the_supported_controls(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    assert music(hass).attributes[ATTR_SUPPORTED_FEATURES] == (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
    )


@pytest.mark.parametrize(
    ("service", "action", "state", "title"),
    [
        ("media_play", "PLAY", "playing", "Morning Light"),
        ("media_pause", "PAUSE", "paused", "Morning Light"),
        ("media_next_track", "NEXT", "paused", "Evening Hum"),
        ("media_previous_track", "PREVIOUS", "paused", "Evening Hum"),
        ("media_stop", "STOP", "idle", None),
    ],
)
async def test_each_control_sends_its_action_and_reads_the_result_back(
    hass: HomeAssistant,
    fake_codie: FakeCodie,
    service: str,
    action: str,
    state: str,
    title: str | None,
) -> None:
    await setup(hass, fake_codie)

    await call(hass, service)

    (write,) = writes(fake_codie)
    assert write.json == {"action": action, "request_id": write.json["request_id"]}
    assert music(hass).state == state
    assert music(hass).attributes.get(ATTR_MEDIA_TITLE) == title


async def test_a_control_waits_for_the_codie_to_run_it(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.requests.clear()
    fake_codie.music_apply_after_reads = 3

    await call(hass, "media_play")

    # One before the command, three while waiting, then the read-back.
    assert len(fake_codie.requests_to("/data/user/music/playback")) == 5
    assert music(hass).state == "playing"


async def test_a_control_the_codie_reports_an_error_for_fails(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.music_error_code = "PLAYBACK_FAILED"

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "media_play")

    assert raised.value.translation_key == "music_command_failed"
    assert raised.value.translation_placeholders == {"error": "PLAYBACK_FAILED"}
    assert music(hass).state == "paused"


async def test_a_control_the_codie_does_not_run_in_time_fails(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.music_apply_after_reads = None

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "media_play")

    assert raised.value.translation_key == "music_command_not_confirmed"
    assert raised.value.translation_placeholders == {"error": "the Music player is paused"}


@pytest.mark.parametrize(
    ("service", "state", "title"),
    [
        ("media_play", "playing", "Morning Light"),
        ("media_pause", "paused", "Morning Light"),
        ("media_next_track", "paused", "Evening Hum"),
        ("media_previous_track", "paused", "Evening Hum"),
        # The Codie leaves out an IDLE status.
        ("media_stop", "idle", None),
    ],
)
async def test_a_control_the_codie_runs_without_saying_so_succeeds(
    hass: HomeAssistant, fake_codie: FakeCodie, service: str, state: str, title: str | None
) -> None:
    fake_codie.music_status = "PLAYING" if service == "media_pause" else "PAUSED"
    await setup(hass, fake_codie)
    fake_codie.music_confirms = False

    await call(hass, service)

    assert music(hass).state == state
    assert music(hass).attributes.get(ATTR_MEDIA_TITLE) == title


async def test_next_without_confirmation_follows_the_queue_entry_not_the_track(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    # The same track twice: only the entry shows that NEXT ran.
    fake_codie.music_queue = [0, 0]
    await setup(hass, fake_codie)
    fake_codie.music_confirms = False

    await call(hass, "media_next_track")

    assert fake_codie.music_entry == 1


async def test_next_without_confirmation_or_a_new_entry_fails(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    # One entry: NEXT comes back to it, so nothing shows that it ran.
    fake_codie.music_queue = [0]
    await setup(hass, fake_codie)
    fake_codie.music_confirms = False

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "media_next_track")

    assert raised.value.translation_key == "music_command_not_confirmed"


async def test_a_control_whose_result_shows_when_the_wait_ends_succeeds(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    fake_codie.music_apply_after_reads = None
    # Something else started the music: the Codie never says it ran ours, but it plays.
    fake_codie.music_status = "PLAYING"

    await call(hass, "media_play")

    assert music(hass).state == "playing"


async def test_a_new_track_starts_at_zero(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)

    # The Codie leaves out a position of 0.
    await call(hass, "media_next_track")

    assert music(hass).attributes[ATTR_MEDIA_POSITION] == 0


async def test_resuming_at_the_same_position_takes_its_time_again(
    hass: HomeAssistant, fake_codie: FakeCodie, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup(hass, fake_codie)

    freezer.tick(30)
    fake_codie.music_status = "PLAYING"
    await refresh(hass, entry)

    # Otherwise the frontend would count on from when it was paused.
    assert music(hass).attributes[ATTR_MEDIA_POSITION] == 30
    assert music(hass).attributes[ATTR_MEDIA_POSITION_UPDATED_AT] == dt_util.utcnow()


async def test_a_control_outside_music_mode_fails_and_says_why(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.music_page_open = False
    await setup(hass, fake_codie)

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "media_play")

    assert raised.value.translation_key == "music_mode_inactive"
    assert music(hass).state == "paused"


async def test_setting_the_volume_sets_the_music_volume(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    await call(hass, "volume_set", volume_level=0.18)

    assert actions(fake_codie) == ["SET_VOLUME"]
    assert fake_codie.music_volume == 18
    assert music(hass).attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.18
    assert state_of(hass, MUSIC_VOLUME) == "18"


async def test_the_volume_works_outside_music_mode(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.music_page_open = False
    await setup(hass, fake_codie)

    await call(hass, "volume_set", volume_level=0.18)

    assert fake_codie.music_volume == 18


@pytest.mark.parametrize(("service", "volume"), [("volume_up", 63), ("volume_down", 45)])
async def test_volume_up_and_down_move_one_step(
    hass: HomeAssistant, fake_codie: FakeCodie, service: str, volume: int
) -> None:
    # 54 is one of the Codie's 11 steps.
    fake_codie.music_volume = 54
    await setup(hass, fake_codie)

    await call(hass, service)

    assert fake_codie.music_volume == volume


async def test_the_player_is_unavailable_unless_the_music_player_is_ready(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry: MockConfigEntry = await setup(hass, fake_codie)

    fake_codie.music_volume_max = 50
    await refresh(hass, entry)

    assert music(hass).state == STATE_UNAVAILABLE

    fake_codie.music_volume_max = 100
    await refresh(hass, entry)

    assert music(hass).state == "paused"
