"""The connectivity sensor for the executor connection."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .const import DOMAIN
from .executor import CodieExecutor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Connected sensor."""
    assert entry.unique_id is not None
    async_add_entities([CodieConnectedSensor(entry.runtime_data.executor, entry.unique_id)])


class CodieConnectedSensor(BinarySensorEntity):
    """On while the executor WebSocket is `ready`."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connected"

    def __init__(self, executor: CodieExecutor, unique_id: str) -> None:
        """Track `executor` for the Codie identified by `unique_id`."""
        self._executor = executor
        self._attr_unique_id = f"{unique_id}_connected"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, unique_id)})

    @property
    def is_on(self) -> bool:
        """Whether the executor socket is ready."""
        return self._executor.connected

    async def async_added_to_hass(self) -> None:
        """Follow connection changes."""
        self.async_on_remove(self._executor.add_listener(self._on_connection_change))

    @callback
    def _on_connection_change(self) -> None:
        """Write the new state."""
        self.async_write_ha_state()
