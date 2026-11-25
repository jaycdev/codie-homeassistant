"""The Voice volume, Music volume and Screen brightness numbers."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .entity import CodieWritableSettingDescription, CodieWritableSettingEntity
from .settings import SettingGroup, async_set_music_volume


@dataclass(frozen=True, kw_only=True)
class CodieNumberDescription(NumberEntityDescription, CodieWritableSettingDescription[int]):
    """A number backed by one of the Codie's settings."""


NUMBERS = (
    CodieNumberDescription(
        key="voice_volume",
        translation_key="voice_volume",
        group=SettingGroup.VOICE,
        value=lambda settings: settings.voice_volume,
        write=lambda client, level: client.async_set_voice_volume(level),
    ),
    CodieNumberDescription(
        key="music_volume",
        translation_key="music_volume",
        group=SettingGroup.MUSIC,
        value=lambda settings: settings.music_volume,
        write=async_set_music_volume,
    ),
    CodieNumberDescription(
        key="screen_brightness",
        translation_key="screen_brightness",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        group=SettingGroup.BRIGHTNESS,
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


class CodieNumber(CodieWritableSettingEntity[int], NumberEntity):
    """A setting of the Codie between 0 and 100."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    entity_description: CodieNumberDescription

    @property
    def native_value(self) -> int | None:
        """The value the Codie reported on the last poll."""
        return self.setting_value

    async def async_set_native_value(self, value: float) -> None:
        """Write `value` and read it back."""
        await self.async_write_setting(round(value))
