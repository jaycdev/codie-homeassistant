"""The Active Character sensor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .entity import CodieSettingDescription, CodieSettingEntity
from .settings import SettingGroup


@dataclass(frozen=True, kw_only=True)
class CodieSensorDescription(SensorEntityDescription, CodieSettingDescription[str]):
    """A sensor backed by one of the Codie's settings."""


ACTIVE_CHARACTER = CodieSensorDescription(
    key="active_character",
    translation_key="active_character",
    group=SettingGroup.CHARACTER,
    value=lambda settings: settings.character,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Active Character sensor."""
    assert entry.unique_id is not None
    async_add_entities(
        [CodieActiveCharacterSensor(entry.runtime_data.settings, entry.unique_id, ACTIVE_CHARACTER)]
    )


class CodieActiveCharacterSensor(CodieSettingEntity[str], SensorEntity):
    """The name of the Character in the active Slot, with the Slot as an attribute."""

    entity_description: CodieSensorDescription

    @property
    def native_value(self) -> str | None:
        """The Character's name; None while it is unknown or has none."""
        return self.setting_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The active Slot's number."""
        settings = self.coordinator.data
        return None if settings is None else {"slot": settings.slot}
