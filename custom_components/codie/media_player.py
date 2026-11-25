"""The Music media player."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityDescription,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .api import CodieMusicPlayback
from .entity import CodieSettingDescription, CodieSettingEntity
from .settings import (
    MUSIC_VOLUME_STEPS,
    CodieSettingsCoordinator,
    SettingGroup,
    async_music_command,
    async_set_music_volume,
)

# The Codie's playback status, and the state it shows as; any other status is idle.
PLAYBACK_STATES = {
    "PLAYING": MediaPlayerState.PLAYING,
    "PAUSED": MediaPlayerState.PAUSED,
    "LOADING": MediaPlayerState.BUFFERING,
}


@dataclass(frozen=True, kw_only=True)
class CodieMusicDescription(MediaPlayerEntityDescription, CodieSettingDescription[int]):
    """The media player, whose setting is the Music volume."""


MUSIC = CodieMusicDescription(
    key="music",
    translation_key="music",
    device_class=MediaPlayerDeviceClass.SPEAKER,
    group=SettingGroup.MUSIC,
    value=lambda settings: settings.music_volume,
    write=async_set_music_volume,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Music media player."""
    assert entry.unique_id is not None
    async_add_entities([CodieMusicPlayer(entry.runtime_data.settings, entry.unique_id, MUSIC)])


class CodieMusicPlayer(CodieSettingEntity[int], MediaPlayerEntity):
    """The Codie's music: what it plays, its controls, and the Music volume."""

    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
    )
    # Volume up and down move one of the Codie's steps.
    _attr_volume_step = 1 / MUSIC_VOLUME_STEPS
    entity_description: CodieMusicDescription
    # What the position was last taken from: the state, the queue entry and the position.
    _position_of: tuple[MediaPlayerState, str | None, int | None] | None = None

    def __init__(
        self,
        coordinator: CodieSettingsCoordinator,
        unique_id: str,
        description: CodieMusicDescription,
    ) -> None:
        """Show the music of the Codie identified by `unique_id`."""
        super().__init__(coordinator, unique_id, description)
        self._update_position()

    @property
    def _playback(self) -> CodieMusicPlayback | None:
        """The music player's state from the last poll; None while it is unknown."""
        settings = self.coordinator.data
        return None if settings is None else settings.music_playback

    @callback
    def _handle_coordinator_update(self) -> None:
        """Take the position from the new poll before the state is written."""
        self._update_position()
        super()._handle_coordinator_update()

    def _update_position(self) -> None:
        """Take the position from the last poll.

        Its time is kept while nothing moved, so a paused track does not change on every poll;
        a new state or track takes the time again even at the same position, since the
        frontend counts on from it while playing.
        """
        playback = self._playback
        position_ms = None if playback is None else playback.position_ms
        position = None if position_ms is None else position_ms // 1000
        entry = None if playback is None else (playback.entry_id or playback.track_id)
        position_of = (self.state, entry, position)
        if position_of != self._position_of:
            settings = self.coordinator.data
            self._position_of = position_of
            self._attr_media_position = position
            self._attr_media_position_updated_at = (
                None if position is None or settings is None else settings.music_position_at
            )

    @property
    def state(self) -> MediaPlayerState:
        """What the music player was doing on the last poll."""
        playback = self._playback
        status = None if playback is None else playback.status
        return PLAYBACK_STATES.get(status or "", MediaPlayerState.IDLE)

    @property
    def volume_level(self) -> float | None:
        """The Music volume, from 0 to 1."""
        volume = self.setting_value
        return None if volume is None else volume / 100

    @property
    def media_title(self) -> str | None:
        """The title of the current track."""
        return None if self._playback is None else self._playback.title

    @property
    def media_artist(self) -> str | None:
        """The artist of the current track."""
        return None if self._playback is None else self._playback.artist

    @property
    def media_album_name(self) -> str | None:
        """The album of the current track."""
        return None if self._playback is None else self._playback.album

    @property
    def media_duration(self) -> int | None:
        """The length of the current track in seconds."""
        duration_ms = None if self._playback is None else self._playback.duration_ms
        return None if duration_ms is None else duration_ms // 1000

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Whether the Codie is in Music mode, when it says."""
        settings = self.coordinator.data
        if settings is None or settings.music_mode is None:
            return None
        return {"music_mode": settings.music_mode}

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the Music volume and read it back."""
        await self.async_write_setting(round(volume * 100))

    async def async_media_play(self) -> None:
        """Play, or resume the current track."""
        await self._async_command("PLAY")

    async def async_media_pause(self) -> None:
        """Pause."""
        await self._async_command("PAUSE")

    async def async_media_stop(self) -> None:
        """Stop and clear the current track; the queue stays."""
        await self._async_command("STOP")

    async def async_media_next_track(self) -> None:
        """Go to the next track."""
        await self._async_command("NEXT")

    async def async_media_previous_track(self) -> None:
        """Go to the previous track."""
        await self._async_command("PREVIOUS")

    async def _async_command(self, action: str) -> None:
        """Run `action` and read the playback back."""
        await self.coordinator.async_write(lambda client: async_music_command(client, action))
