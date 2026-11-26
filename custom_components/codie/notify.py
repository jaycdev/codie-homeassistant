"""The Announce and Announce in character notify entities."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodieConfigEntry
from .announce import async_announce, async_announce_in_character
from .api import CodieClient
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Announce and Announce in character entities."""
    assert entry.unique_id is not None
    async_add_entities(
        [
            CodieAnnounceEntity(
                entry.runtime_data.client,
                entry.unique_id,
                partial(entry.async_start_reauth, hass),
            ),
            CodieAnnounceInCharacterEntity(entry),
        ]
    )


class CodieAnnounceEntity(NotifyEntity):
    """`notify.send_message` makes the Character speak the message at once."""

    _attr_has_entity_name = True
    _attr_translation_key = "announce"

    def __init__(
        self, client: CodieClient, unique_id: str, on_auth_failed: Callable[[], None]
    ) -> None:
        """Announce through `client`, for the Codie identified by `unique_id`.

        `on_auth_failed` starts re-authentication when the Codie rejects the credentials.
        """
        self._client = client
        self._on_auth_failed = on_auth_failed
        self._attr_unique_id = f"{unique_id}_announce"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, unique_id)})

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Speak `message`. A title is ignored: the Character speaks only the text."""
        await async_announce(self._client, message, self._on_auth_failed)


class CodieAnnounceInCharacterEntity(NotifyEntity):
    """`notify.send_message` makes the Character say the message in its own words."""

    _attr_has_entity_name = True
    _attr_translation_key = "announce_in_character"

    def __init__(self, entry: CodieConfigEntry) -> None:
        """Say it through the Codie of `entry`."""
        assert entry.unique_id is not None
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}_announce_in_character"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Say `message` in character, in the default wording. A title is ignored."""
        await async_announce_in_character(self.hass, self._entry, message)
