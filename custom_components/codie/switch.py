"""The screen and side light switches."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .api import CodieClient
from .entity import CodieSettingEntity
from .settings import GROUP_LIGHTS, GROUP_SCREEN, CodieSettings, CodieSettingsCoordinator


@dataclass(frozen=True, kw_only=True)
class CodieSwitchDescription(SwitchEntityDescription):
    """A switch backed by one of the Codie's settings."""

    group: str
    value: Callable[[CodieSettings], bool | None]
    write: Callable[[CodieClient, bool], Awaitable[None]]


SWITCHES = (
    CodieSwitchDescription(
        key="screen",
        translation_key="screen",
        group=GROUP_SCREEN,
        value=lambda settings: settings.screen_on,
        write=lambda client, on: client.async_set_screen(on),
    ),
    CodieSwitchDescription(
        key="side_lights",
        translation_key="side_lights",
        entity_category=EntityCategory.CONFIG,
        group=GROUP_LIGHTS,
        value=lambda settings: settings.side_lights,
        write=lambda client, on: client.async_set_side_lights(on),
    ),
    CodieSwitchDescription(
        key="music_side_lights",
        translation_key="music_side_lights",
        entity_category=EntityCategory.CONFIG,
        group=GROUP_LIGHTS,
        value=lambda settings: settings.music_side_lights,
        write=lambda client, on: client.async_set_music_side_lights(on),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Screen, Side lights and Music side lights switches."""
    assert entry.unique_id is not None
    async_add_entities(
        CodieSwitch(entry.runtime_data.settings, entry.unique_id, description)
        for description in SWITCHES
    )


class CodieSwitch(CodieSettingEntity, SwitchEntity):
    """An on/off setting of the Codie."""

    entity_description: CodieSwitchDescription

    def __init__(
        self,
        coordinator: CodieSettingsCoordinator,
        unique_id: str,
        description: CodieSwitchDescription,
    ) -> None:
        """Show `description`'s setting of the Codie identified by `unique_id`."""
        super().__init__(coordinator, unique_id, description, description.group)

    @property
    def is_on(self) -> bool | None:
        """The state the Codie reported on the last poll; None while it is unknown."""
        settings = self.settings
        return None if settings is None else self.entity_description.value(settings)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch on and read it back."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch off and read it back."""
        await self._set(False)

    async def _set(self, on: bool) -> None:
        write = self.entity_description.write
        await self.coordinator.async_write(lambda client: write(client, on))
