"""What the Codie's setting entities have in common."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .settings import CodieSettings, CodieSettingsCoordinator


class CodieSettingEntity(CoordinatorEntity[CodieSettingsCoordinator]):
    """An entity showing one setting of the Codie, from the shared poll."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CodieSettingsCoordinator,
        unique_id: str,
        description: EntityDescription,
        group: str,
    ) -> None:
        """Show `description`'s setting of the Codie identified by `unique_id`.

        `group` is the settings group the entity is read with; it is unavailable while that
        group is.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._group = group
        self._attr_unique_id = f"{unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, unique_id)})

    @property
    def settings(self) -> CodieSettings | None:
        """The last poll, or None if there has not been a successful one yet."""
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Unavailable while the Codie cannot be reached, or cannot give this setting."""
        settings = self.settings
        return (
            super().available and settings is not None and self._group not in settings.unavailable
        )
