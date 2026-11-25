"""The Character select, which switches the active Slot."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .const import DOMAIN
from .entity import CodieWritableSettingDescription, CodieWritableSettingEntity
from .settings import SettingGroup


@dataclass(frozen=True, kw_only=True)
class CodieSelectDescription(SelectEntityDescription, CodieWritableSettingDescription[int]):
    """A select backed by one of the Codie's settings."""


CHARACTER = CodieSelectDescription(
    key="character",
    translation_key="character",
    group=SettingGroup.CHARACTER,
    value=lambda settings: settings.slot,
    write=lambda client, slot: client.async_set_slot(slot),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Character select."""
    assert entry.unique_id is not None
    async_add_entities(
        [CodieCharacterSelect(entry.runtime_data.settings, entry.unique_id, CHARACTER)]
    )


class CodieCharacterSelect(CodieWritableSettingEntity[int], SelectEntity):
    """One option per Slot, named after its Character; choosing one makes that Slot active."""

    entity_description: CodieSelectDescription

    @property
    def options(self) -> list[str]:
        """The Slots the Codie reported on the last poll."""
        settings = self.coordinator.data
        return [] if settings is None else list(settings.character_options)

    @property
    def current_option(self) -> str | None:
        """The option of the active Slot."""
        settings = self.coordinator.data
        return None if settings is None else settings.character_option

    async def async_select_option(self, option: str) -> None:
        """Switch to the Slot of `option`, then read the settings back."""
        settings = self.coordinator.data
        slot = None if settings is None else settings.character_options.get(option)
        if slot is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="character_unknown",
                translation_placeholders={"option": option},
            )
        await self.async_write_setting(slot)
