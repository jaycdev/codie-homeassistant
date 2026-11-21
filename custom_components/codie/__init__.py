"""The CODE27 Codie integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CodieClient, CodieCredentials, CodieError
from .const import (
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
    DOMAIN,
)
from .executor import CodieExecutor
from .tools import async_call_tool, async_register_catalog

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR]


@dataclass
class CodieRuntimeData:
    """What a loaded entry keeps in memory."""

    client: CodieClient
    executor: CodieExecutor


type CodieConfigEntry = ConfigEntry[CodieRuntimeData]


def _client_for(hass: HomeAssistant, entry: ConfigEntry[Any]) -> CodieClient:
    return CodieClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        CodieCredentials(
            local_secret=entry.data[CONF_LOCAL_SECRET],
            user_id=entry.data[CONF_USER_ID],
            local_session_id=entry.data.get(CONF_LOCAL_SESSION_ID, ""),
        ),
    )


async def async_setup_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Set up a Codie from a config entry."""
    if entry.unique_id is None:
        raise ConfigEntryError("The Codie entry has no unique ID; remove and add it again")
    client = _client_for(hass, entry)
    integration_id: str = entry.data[CONF_INTEGRATION_ID]

    try:
        await async_register_catalog(client, integration_id)
    except CodieError as err:
        raise ConfigEntryNotReady(f"Cannot register tools on the Codie: {err}") from err

    executor = CodieExecutor(client, integration_id, partial(async_call_tool, hass))
    entry.runtime_data = CodieRuntimeData(client=client, executor=executor)

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id)},
        manufacturer="CODE27",
        model="Codie",
        name=entry.title,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(executor.async_close)
    entry.async_create_background_task(hass, executor.async_run(), "codie executor")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Delete the tool integration from the Codie."""
    try:
        await _client_for(hass, entry).async_delete_tool_integration(
            entry.data[CONF_INTEGRATION_ID]
        )
    except CodieError as err:
        _LOGGER.warning("Could not delete the tool integration from the Codie: %s", err)
