"""The Announce notify entity."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .announce import async_announce
from .api import CodieClient
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Announce entity."""
    assert entry.unique_id is not None
    async_add_entities([CodieAnnounceEntity(entry.runtime_data.client, entry.unique_id)])


class CodieAnnounceEntity(NotifyEntity):
    """`notify.send_message` makes the Character speak the message at once."""

    _attr_has_entity_name = True
    _attr_translation_key = "announce"

    def __init__(self, client: CodieClient, unique_id: str) -> None:
        """Announce through `client`, for the Codie identified by `unique_id`."""
        self._client = client
        self._attr_unique_id = f"{unique_id}_announce"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, unique_id)})

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Speak `message`. A title is ignored: the Character speaks only the text."""
        await async_announce(self._client, message)
