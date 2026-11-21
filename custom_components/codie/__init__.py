"""The CODE27 Codie integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CodieClient, CodieCredentials
from .const import CONF_LOCAL_SECRET, CONF_LOCAL_SESSION_ID, CONF_USER_ID, DOMAIN


@dataclass
class CodieRuntimeData:
    """What a loaded entry keeps in memory."""

    client: CodieClient


type CodieConfigEntry = ConfigEntry[CodieRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Set up a Codie from a config entry."""
    entry.runtime_data = CodieRuntimeData(
        client=CodieClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            CodieCredentials(
                local_secret=entry.data[CONF_LOCAL_SECRET],
                user_id=entry.data[CONF_USER_ID],
                local_session_id=entry.data.get(CONF_LOCAL_SESSION_ID, ""),
            ),
        )
    )

    if entry.unique_id is None:
        raise ConfigEntryError("The Codie entry has no unique ID; remove and add it again")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id)},
        manufacturer="CODE27",
        model="Codie",
        name=entry.title,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Unload a config entry."""
    return True
