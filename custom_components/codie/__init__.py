"""The CODE27 Codie integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_COMPONENT_LOADED, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.typing import ConfigType

from .announce import async_register_services
from .api import CodieAuthError, CodieClient, CodieCredentials, CodieError
from .const import (
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
    DOMAIN,
)
from .executor import CodieExecutor
from .tools import CatalogRegistrar, async_call_tool

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NOTIFY]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Components load in bursts at startup; wait for the burst to end before comparing catalogs.
REFRESH_COOLDOWN_S = 10.0


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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Set up a Codie from a config entry."""
    if entry.unique_id is None:
        raise ConfigEntryError("The Codie entry has no unique ID; remove and add it again")
    client = _client_for(hass, entry)
    integration_id: str = entry.data[CONF_INTEGRATION_ID]

    registrar = CatalogRegistrar(hass, client, integration_id)
    try:
        await registrar.async_register()
    except CodieAuthError as err:
        raise ConfigEntryAuthFailed(f"The Codie rejected our credentials: {err}") from err
    except CodieError as err:
        raise ConfigEntryNotReady(f"Cannot register tools on the Codie: {err}") from err

    # The Character speaks the AI language, not the device's UI language. It is read once here.
    try:
        system = await client.async_get_system()
    except CodieAuthError as err:
        raise ConfigEntryAuthFailed(f"The Codie rejected our credentials: {err}") from err
    except CodieError as err:
        raise ConfigEntryNotReady(f"Cannot read the Codie's language: {err}") from err
    ai_language = system.get("ai_language")
    language = ai_language if isinstance(ai_language, str) and ai_language else None

    executor = CodieExecutor(
        client,
        integration_id,
        partial(async_call_tool, hass, language),
        on_auth_failed=lambda: entry.async_start_reauth(hass),
    )
    entry.runtime_data = CodieRuntimeData(client=client, executor=executor)

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id)},
        manufacturer="CODE27",
        model="Codie",
        name=entry.title,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def refresh_catalog() -> None:
        """Re-register after the Assist tools changed, then reconnect to pick it up."""
        try:
            if await registrar.async_refresh():
                _LOGGER.info("The available Assist tools changed; reconnecting the executor")
                await executor.async_reconnect()
        except CodieAuthError:
            entry.async_start_reauth(hass)
        except CodieError as err:
            # The next loaded component tries again.
            _LOGGER.warning("Could not refresh the tools on the Codie: %s", err)

    # An integration that loads after this entry, or an upgrade, adds or removes Assist tools.
    debouncer = Debouncer(
        hass, _LOGGER, cooldown=REFRESH_COOLDOWN_S, immediate=False, function=refresh_catalog
    )
    entry.async_on_unload(debouncer.async_shutdown)
    entry.async_on_unload(
        hass.bus.async_listen(
            EVENT_COMPONENT_LOADED, lambda _event: debouncer.async_schedule_call()
        )
    )
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
