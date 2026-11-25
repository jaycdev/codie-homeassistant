"""Reads the Codie's settings together, and writes them one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CodieApiError,
    CodieAuthError,
    CodieClient,
    CodieConnectionError,
    CodieError,
    CodieMusicPlayback,
    CodieNotConfirmedError,
    CodieResponseError,
)
from .const import DOMAIN
from .errors import failed, unreachable_error

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=30)
# The Codie only queues a music command. Ask how it went this often, for this long; if it has
# not said by then, the playback it reports decides.
MUSIC_CONFIRM_INTERVAL_S = 0.5
MUSIC_CONFIRM_TIMEOUT_S = 3.0
# The Codie has this many Music volume steps above 0 and rounds a level down to one, so it
# reports 27 for step 3 but takes 27 as step 2. Measured on a Codie; the API does not say.
MUSIC_VOLUME_STEPS = 11


class SettingGroup(StrEnum):
    """The settings are read in groups that fail on their own, and each entity belongs to one."""

    CHARACTER = "character"
    VOICE = "voice"
    MUSIC = "music"
    MUSIC_MODE = "music_mode"
    BRIGHTNESS = "brightness"
    SCREEN = "screen"
    LIGHTS = "lights"


class CodieMusicCommandError(CodieError):
    """The Codie took the music command `action` but could not run it."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(reason)
        self.action = action
        self.reason = reason


class CodieMusicCommandNotConfirmedError(CodieError):
    """The Codie did not say it ran the music command `action`, and the playback doesn't show it."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(reason)
        self.action = action


@dataclass(frozen=True)
class CodieSettings:
    """What the Codie reported on one poll. None means it did not say."""

    # The active Slot, and the name of the Character in each Slot ("" if it has none).
    slot: int | None = None
    characters: Mapping[int, str] = field(default_factory=dict)
    # Voice volume of the Character in the active Slot.
    voice_volume: int | None = None
    music_volume: int | None = None
    brightness: int | None = None
    screen_on: bool | None = None
    side_lights: bool | None = None
    music_side_lights: bool | None = None
    # The music player's state, None while it is unknown, and when its position was right.
    music_playback: CodieMusicPlayback | None = None
    music_position_at: datetime | None = None
    music_mode: bool | None = None
    # The groups that cannot be shown: their read failed, or the Codie says they cannot be used
    # (the music player is not ready, or the brightness cannot be set).
    unavailable: frozenset[SettingGroup] = frozenset()

    @property
    def character(self) -> str | None:
        """The name of the Character in the active Slot."""
        return None if self.slot is None else self.characters.get(self.slot) or None

    @property
    def character_options(self) -> dict[str, int]:
        """One option per Slot, named after its Character, mapped to the Slot number.

        Two Slots can hold the same Character, so a name that is not unique gets its Slot.
        """
        counts = Counter(self.characters.values())
        options: dict[str, int] = {}
        for slot, name in sorted(self.characters.items()):
            if not name:
                options[f"Slot {slot}"] = slot
            elif counts[name] > 1:
                options[f"{name} (Slot {slot})"] = slot
            else:
                options[name] = slot
        return options

    @property
    def character_option(self) -> str | None:
        """The option of the active Slot."""
        return next(
            (option for option, slot in self.character_options.items() if slot == self.slot),
            None,
        )


def _write_error(err: CodieError) -> HomeAssistantError:
    """Turn a failed write into a Home Assistant error."""
    if (unreachable := unreachable_error(err, "setting")) is not None:
        return unreachable
    if isinstance(err, CodieMusicCommandNotConfirmedError) and err.action != "SET_VOLUME":
        return failed("music_command_not_confirmed", error=str(err))
    if isinstance(err, CodieNotConfirmedError | CodieMusicCommandNotConfirmedError):
        return failed("setting_not_confirmed", error=str(err))
    if isinstance(err, CodieMusicCommandError):
        if err.reason == "MODE_INACTIVE":
            # The Codie can also refuse a queued command outside Music mode once it gets to it.
            return failed("music_mode_inactive")
        if err.action == "SET_VOLUME":
            return failed("setting_music_volume_failed", error=err.reason)
        return failed("music_command_failed", error=err.reason)
    if isinstance(err, CodieApiError) and err.code == "MODE_INACTIVE":
        return failed("music_mode_inactive")
    return failed("setting_failed", error=str(err))


def _music_position_at(playback: CodieMusicPlayback, read_at: datetime) -> datetime:
    """When the position in `playback`, read at `read_at`, was right.

    That is the Codie's own time for the snapshot when it is believable, and otherwise when it
    was read. The Codie's clock can differ from ours, so a time after the read, or one that
    puts a playing track past its end by the read, is not believed.
    """
    if playback.updated_at_ms is None:
        return read_at
    updated_at = dt_util.utc_from_timestamp(playback.updated_at_ms / 1000)
    if updated_at > read_at:
        return read_at
    if (
        playback.status == "PLAYING"
        and playback.duration_ms is not None
        and playback.position_ms is not None
        and playback.position_ms + (read_at - updated_at).total_seconds() * 1000
        > playback.duration_ms
    ):
        return read_at
    return updated_at


def _music_step(volume: int) -> int:
    """The Codie's Music volume step nearest to `volume`."""
    return round(volume * MUSIC_VOLUME_STEPS / 100)


# Given the playback from before a music command and the latest one, why the command does not
# show as run, or None if it does.
type MusicCommandCheck = Callable[[CodieMusicPlayback, CodieMusicPlayback], str | None]


async def _async_run_music_command(
    client: CodieClient, action: str, fields: dict[str, int], settled: MusicCommandCheck
) -> None:
    """Send the music command `action` with `fields`, and ask until the Codie says it ran.

    A command it reports as applied is a success whatever the playback shows.
    Raises `CodieMusicCommandError` if it reports an error for this command. A command that
    another one replaced is left to that one. If the Codie has said nothing by the deadline,
    this succeeds only if `settled` finds the command shows in the playback, and otherwise
    raises `CodieMusicCommandNotConfirmedError` with the reason `settled` gave.
    """
    # The command the Codie last took, so that an older one is not taken for a newer one.
    before = await client.async_get_music_playback()
    request_id = await client.async_music_control(action, **fields)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MUSIC_CONFIRM_TIMEOUT_S
    while True:
        await asyncio.sleep(MUSIC_CONFIRM_INTERVAL_S)
        playback = await client.async_get_music_playback()
        if playback.request_id == request_id:
            if playback.error_code:
                raise CodieMusicCommandError(action, playback.error_code)
            if playback.applied:
                return
        elif playback.request_id not in (None, before.request_id):
            # Another command replaced ours; its own wait reports how that went.
            return
        if loop.time() >= deadline:
            if (reason := settled(before, playback)) is None:
                return
            raise CodieMusicCommandNotConfirmedError(action, reason)


async def async_set_music_volume(client: CodieClient, volume: int) -> None:
    """Set the Music volume to the Codie's step nearest `volume`, and ask whether it ran.

    The level sent is the lowest one in that step, since the Codie rounds down; the level it
    reports afterwards may still differ from `volume`. If the Codie has not said whether it ran
    by the deadline, this succeeds only when the Music volume it reports is already in that
    step. See `_async_run_music_command` for the rest.
    """
    step = _music_step(volume)

    def settled(_before: CodieMusicPlayback, playback: CodieMusicPlayback) -> str | None:
        if playback.volume is None:
            return "the Music player is not ready"
        if _music_step(playback.volume) != step:
            return f"the Music volume is {playback.volume}, not {volume}"
        return None

    level = -(-step * 100 // MUSIC_VOLUME_STEPS)
    await _async_run_music_command(client, "SET_VOLUME", {"volume_level": level}, settled)


def _status_is(*statuses: str) -> MusicCommandCheck:
    """A `MusicCommandCheck` that looks for the music player's status to be one of `statuses`."""

    def settled(_before: CodieMusicPlayback, playback: CodieMusicPlayback) -> str | None:
        # The Codie leaves out a status that is IDLE.
        status = playback.status or "IDLE"
        return None if status in statuses else f"the Music player is {status.lower()}"

    return settled


def _track_changed(before: CodieMusicPlayback, playback: CodieMusicPlayback) -> str | None:
    """A `MusicCommandCheck` that looks for the queue entry to differ from the one before.

    The entry, not the track, since a track can be in the queue more than once. Without a
    queue the track decides.
    """
    if (playback.entry_id, playback.track_id) != (before.entry_id, before.track_id):
        return None
    return "the track did not change"


# How the playback shows that each transport command ran, should the Codie not say.
_MUSIC_COMMAND_CHECKS: dict[str, MusicCommandCheck] = {
    "PLAY": _status_is("PLAYING", "LOADING"),
    "PAUSE": _status_is("PAUSED"),
    "STOP": _status_is("IDLE", "COMPLETED"),
    "NEXT": _track_changed,
    "PREVIOUS": _track_changed,
}


async def async_music_command(client: CodieClient, action: str) -> None:
    """Run the transport command `action`: PLAY, PAUSE, STOP, NEXT or PREVIOUS.

    The Codie only takes it in Music mode. See `_async_run_music_command` for how it is
    confirmed.
    """
    await _async_run_music_command(client, action, {}, _MUSIC_COMMAND_CHECKS[action])


class CodieSettingsCoordinator(DataUpdateCoordinator[CodieSettings]):
    """Polls the settings every 30 s, and reads them again right after a write."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: CodieClient) -> None:
        """Poll `client`, the Codie of `entry`."""
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self._client = client

    async def _async_update_data(self) -> CodieSettings:
        client = self._client
        unavailable: set[SettingGroup] = set()

        async def read[T](group: SettingGroup, reader: Callable[[], Awaitable[T]]) -> T | None:
            """Read one group. One that the Codie answers with an error is left out."""
            try:
                return await reader()
            except CodieConnectionError, CodieAuthError:
                raise
            except (CodieApiError, CodieResponseError) as err:
                _LOGGER.debug("Cannot read the %s settings: %s", group, err)
                unavailable.add(group)
                return None

        try:
            slot = await read(SettingGroup.CHARACTER, client.async_get_slot)
            characters = (
                None
                if slot is None
                else await read(SettingGroup.CHARACTER, client.async_get_characters)
            )
            playback = await read(SettingGroup.MUSIC, client.async_get_music_playback)
            music_read_at = dt_util.utcnow()
            music_mode = await read(SettingGroup.MUSIC_MODE, client.async_get_music_mode)
            brightness = await read(SettingGroup.BRIGHTNESS, client.async_get_brightness)
            screen_on = await read(SettingGroup.SCREEN, client.async_get_screen)
            lights = await read(SettingGroup.LIGHTS, client.async_get_light_switches)
        except CodieAuthError as err:
            raise ConfigEntryAuthFailed(f"The Codie rejected our credentials: {err}") from err
        except CodieError as err:
            raise UpdateFailed(f"Cannot read the Codie's settings: {err}") from err
        # Voice volume belongs to the active Slot, and comes with the Character list.
        active = characters.get(slot) if characters and slot is not None else None
        if active is None:
            unavailable.add(SettingGroup.VOICE)
        if unavailable == set(SettingGroup):
            raise UpdateFailed("The Codie answered every settings request with an error")

        if not characters:
            # Without the Characters there is nothing to name or to switch to.
            unavailable.add(SettingGroup.CHARACTER)
        if playback and playback.volume is None:
            unavailable.add(SettingGroup.MUSIC)
        if brightness and (brightness.level is None or not brightness.writable):
            unavailable.add(SettingGroup.BRIGHTNESS)
        return CodieSettings(
            slot=slot,
            characters={number: c.name for number, c in (characters or {}).items()},
            voice_volume=active.voice_volume if active else None,
            music_volume=playback.volume if playback else None,
            brightness=brightness.level if brightness else None,
            screen_on=screen_on,
            side_lights=lights.side_lights if lights else None,
            music_side_lights=lights.music_side_lights if lights else None,
            music_playback=playback,
            music_position_at=_music_position_at(playback, music_read_at) if playback else None,
            music_mode=music_mode,
            unavailable=frozenset(unavailable),
        )

    async def async_write(self, write: Callable[[CodieClient], Awaitable[None]]) -> None:
        """Run `write` against the Codie, then read the settings back.

        Raises `HomeAssistantError` if the Codie refused or could not be reached; the settings
        are read back then too, so the entities show what the Codie really has. A Codie that
        rejects the credentials also starts re-authentication.
        """
        try:
            await write(self._client)
        except CodieAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise _write_error(err) from err
        except CodieError as err:
            raise _write_error(err) from err
        finally:
            await self.async_refresh()
