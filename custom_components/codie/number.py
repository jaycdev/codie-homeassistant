"""The volume and brightness numbers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .api import CodieClient
from .entity import CodieSettingEntity
from .settings import (
    GROUP_BRIGHTNESS,
    GROUP_MUSIC,
    GROUP_VOICE,
    CodieSettings,
    CodieSettingsCoordinator,
    async_set_music_volume,
)


@dataclass(frozen=True, kw_only=True)
class CodieNumberDescription(NumberEntityDescription):
    """A number backed by one of the Codie's settings."""

    group: str
    value: Callable[[CodieSettings], int | None]
    write: Callable[[CodieClient, int], Awaitable[None]]


NUMBERS = (
    CodieNumberDescription(
        key="voice_volume",
        translation_key="voice_volume",
        group=GROUP_VOICE,
        value=lambda settings: settings.voice_volume,
        write=lambda client, level: client.async_set_voice_volume(level),
    ),
    CodieNumberDescription(
        key="music_volume",
        translation_key="music_volume",
        group=GROUP_MUSIC,
        value=lambda settings: settings.music_volume,
        write=async_set_music_volume,
    ),
    CodieNumberDescription(
        key="screen_brightness",
        translation_key="screen_brightness",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        group=GROUP_BRIGHTNESS,
        value=lambda settings: settings.brightness,
        write=lambda client, level: client.async_set_brightness(level),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Voice volume, Music volume and Screen brightness numbers."""
    assert entry.unique_id is not None
    async_add_entities(
        CodieNumber(entry.runtime_data.settings, entry.unique_id, description)
        for description in NUMBERS
    )


class CodieNumber(CodieSettingEntity, NumberEntity):
    """A setting of the Codie between 0 and 100."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    entity_description: CodieNumberDescription

    def __init__(
        self,
        coordinator: CodieSettingsCoordinator,
        unique_id: str,
        description: CodieNumberDescription,
    ) -> None:
        """Show `description`'s setting of the Codie identified by `unique_id`."""
        super().__init__(coordinator, unique_id, description, description.group)

    @property
    def native_value(self) -> int | None:
        """The value the Codie reported on the last poll."""
        settings = self.settings
        return None if settings is None else self.entity_description.value(settings)

    async def async_set_native_value(self, value: float) -> None:
        """Write `value` and read it back."""
        write = self.entity_description.write
        await self.coordinator.async_write(lambda client: write(client, round(value)))
