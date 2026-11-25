"""What the Codie's setting entities have in common."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import CodieClient
from .const import DOMAIN
from .settings import CodieSettings, CodieSettingsCoordinator, SettingGroup


@dataclass(frozen=True, kw_only=True)
class CodieSettingDescription[V](EntityDescription):
    """An entity description backed by one of the Codie's settings, whose value is a `V`."""

    # The group the setting is read with; the entity is unavailable while that group is.
    group: SettingGroup
    value: Callable[[CodieSettings], V | None]


@dataclass(frozen=True, kw_only=True)
class CodieWritableSettingDescription[V](CodieSettingDescription[V]):
    """A setting the entity can also change, by writing a `V`."""

    write: Callable[[CodieClient, V], Awaitable[None]]


class CodieSettingEntity[V](CoordinatorEntity[CodieSettingsCoordinator]):
    """An entity showing one setting of the Codie, from the shared poll."""

    _attr_has_entity_name = True
    entity_description: CodieSettingDescription[V]

    def __init__(
        self,
        coordinator: CodieSettingsCoordinator,
        unique_id: str,
        description: CodieSettingDescription[V],
    ) -> None:
        """Show `description`'s setting of the Codie identified by `unique_id`."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, unique_id)})

    @property
    def available(self) -> bool:
        """Unavailable while the Codie cannot be reached, or cannot give this setting."""
        settings = self.coordinator.data
        return (
            super().available
            and settings is not None
            and self.entity_description.group not in settings.unavailable
        )

    @property
    def setting_value(self) -> V | None:
        """The value the Codie reported on the last poll; None while it is unknown."""
        settings = self.coordinator.data
        return None if settings is None else self.entity_description.value(settings)


class CodieWritableSettingEntity[V](CodieSettingEntity[V]):
    """An entity showing one setting of the Codie, which it can also change."""

    entity_description: CodieWritableSettingDescription[V]

    async def async_write_setting(self, value: V) -> None:
        """Write `value` and read it back."""
        write = self.entity_description.write
        await self.coordinator.async_write(lambda client: write(client, value))
