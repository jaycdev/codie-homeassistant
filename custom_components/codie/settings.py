"""Reads the Codie's settings together, and writes them one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CodieApiError,
    CodieAuthError,
    CodieClient,
    CodieConnectionError,
    CodieError,
    CodieNotConfirmedError,
    CodieResponseError,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=30)
# After a Music volume change the Codie only queues the command. Ask how it went this often,
# for this long; if it does not say in time, the next poll shows the value that took effect.
MUSIC_CONFIRM_INTERVAL_S = 0.5
MUSIC_CONFIRM_TIMEOUT_S = 3.0

# The settings are read in groups that fail on their own, and each entity belongs to one.
GROUP_VOICE = "voice"
GROUP_MUSIC = "music"
GROUP_BRIGHTNESS = "brightness"
GROUP_SCREEN = "screen"
GROUP_LIGHTS = "lights"
GROUPS = frozenset({GROUP_VOICE, GROUP_MUSIC, GROUP_BRIGHTNESS, GROUP_SCREEN, GROUP_LIGHTS})


class CodieMusicVolumeError(CodieError):
    """The Codie took the Music volume command but could not run it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CodieSettings:
    """What the Codie reported on one poll. None means it did not say."""

    # Voice volume of the Character in the active Slot.
    voice_volume: int | None = None
    music_volume: int | None = None
    brightness: int | None = None
    screen_on: bool | None = None
    side_lights: bool | None = None
    music_side_lights: bool | None = None
    # The groups that cannot be shown: their read failed, or the Codie says they cannot be used
    # (the music player is not ready, or the brightness cannot be set).
    unavailable: frozenset[str] = frozenset()


def _failed(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def _write_error(err: CodieError) -> HomeAssistantError:
    """Turn a failed write into a Home Assistant error."""
    if isinstance(err, CodieConnectionError):
        return _failed("setting_cannot_connect")
    if isinstance(err, CodieAuthError):
        return _failed("setting_auth_failed")
    if isinstance(err, CodieNotConfirmedError):
        return _failed("setting_not_confirmed", error=str(err))
    if isinstance(err, CodieMusicVolumeError):
        return _failed("setting_music_volume_failed", error=err.code)
    return _failed("setting_failed", error=str(err))


async def async_set_music_volume(client: CodieClient, volume: int) -> None:
    """Set the Music volume, and wait a little to hear whether the Codie ran the command.

    Raises `CodieMusicVolumeError` if it reports an error for this command. If it says nothing
    in time that is not an error: the command may still be running.
    """
    request_id = await client.async_set_music_volume(volume)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MUSIC_CONFIRM_TIMEOUT_S
    while True:
        await asyncio.sleep(MUSIC_CONFIRM_INTERVAL_S)
        playback = await client.async_get_music_playback()
        if playback.request_id == request_id:
            if playback.error_code:
                raise CodieMusicVolumeError(playback.error_code)
            if playback.applied:
                return
        if loop.time() >= deadline:
            return


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
        unavailable: set[str] = set()

        async def read[T](group: str, reader: Callable[[], Awaitable[T]]) -> T | None:
            """Read one group. One that the Codie answers with an error is left out."""
            try:
                return await reader()
            except CodieConnectionError, CodieAuthError:
                raise
            except (CodieApiError, CodieResponseError) as err:
                _LOGGER.debug("Cannot read the %s settings: %s", group, err)
                unavailable.add(group)
                return None

        async def read_voice_volume() -> int | None:
            # Voice volume belongs to the Slot, so the Slot is read first.
            return await client.async_get_voice_volume(await client.async_get_slot())

        try:
            voice_volume = await read(GROUP_VOICE, read_voice_volume)
            playback = await read(GROUP_MUSIC, client.async_get_music_playback)
            brightness = await read(GROUP_BRIGHTNESS, client.async_get_brightness)
            screen_on = await read(GROUP_SCREEN, client.async_get_screen)
            lights = await read(GROUP_LIGHTS, client.async_get_light_switches)
        except CodieAuthError as err:
            raise ConfigEntryAuthFailed(f"The Codie rejected our credentials: {err}") from err
        except CodieError as err:
            raise UpdateFailed(f"Cannot read the Codie's settings: {err}") from err
        if unavailable == GROUPS:
            raise UpdateFailed("The Codie answered every settings request with an error")

        if playback and playback.volume is None:
            unavailable.add(GROUP_MUSIC)
        if brightness and (brightness.level is None or not brightness.writable):
            unavailable.add(GROUP_BRIGHTNESS)
        return CodieSettings(
            voice_volume=voice_volume,
            music_volume=playback.volume if playback else None,
            brightness=brightness.level if brightness else None,
            screen_on=screen_on,
            side_lights=lights.side_lights if lights else None,
            music_side_lights=lights.music_side_lights if lights else None,
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
