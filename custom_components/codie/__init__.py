"""The CODE27 Codie integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_listen_entity_updates
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_COMPONENT_LOADED, Platform
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType

from .announce import async_announce, async_register_services
from .api import CodieAuthError, CodieClient, CodieCredentials, CodieError
from .const import (
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
    DOMAIN,
)
from .executor import CodieExecutor
from .issues import async_set_clock_skew
from .settings import CodieSettingsCoordinator
from .timers import CodieTimers, async_remove_timers, timers_store
from .tool_events import ToolRecorder
from .tools import CatalogRegistrar, async_call_tool

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.MEDIA_PLAYER,
    Platform.NOTIFY,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Components load in bursts at startup; wait for the burst to end before comparing catalogs.
REFRESH_COOLDOWN_S = 10.0
# A refresh that failed is tried again after this long, even if no other component loads.
REFRESH_RETRY_S = 300.0


@dataclass
class CodieRuntimeData:
    """What a loaded entry keeps in memory."""

    client: CodieClient
    executor: CodieExecutor
    settings: CodieSettingsCoordinator


type CodieConfigEntry = ConfigEntry[CodieRuntimeData]


def _client_for(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    on_clock_skew: Callable[[bool], None] | None = None,
) -> CodieClient:
    return CodieClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        CodieCredentials(
            local_secret=entry.data[CONF_LOCAL_SECRET],
            user_id=entry.data[CONF_USER_ID],
            local_session_id=entry.data.get(CONF_LOCAL_SESSION_ID, ""),
        ),
        on_clock_skew,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Set up a Codie from a config entry."""
    if entry.unique_id is None:
        raise ConfigEntryError("The Codie entry has no unique ID; remove and add it again")
    client = _client_for(hass, entry, partial(async_set_clock_skew, hass, entry))
    integration_id: str = entry.data[CONF_INTEGRATION_ID]

    # The Character speaks the AI language, not the device's UI language. It is read once here,
    # before the catalog, because the Timer Tools depend on it.
    try:
        system = await client.async_get_system()
    except CodieAuthError as err:
        raise ConfigEntryAuthFailed(f"The Codie rejected our credentials: {err}") from err
    except CodieError as err:
        raise ConfigEntryNotReady(f"Cannot read the Codie's language: {err}") from err
    ai_language = system.get("ai_language")
    language = ai_language if isinstance(ai_language, str) and ai_language else None

    registrar = CatalogRegistrar(hass, client, entry, language)
    try:
        await registrar.async_register()
    except CodieAuthError as err:
        raise ConfigEntryAuthFailed(f"The Codie rejected our credentials: {err}") from err
    except CodieError as err:
        raise ConfigEntryNotReady(f"Cannot register tools on the Codie: {err}") from err

    async def announce(message: str) -> None:
        await async_announce(client, message, partial(entry.async_start_reauth, hass))

    timers = CodieTimers(hass, timers_store(hass, entry.entry_id), language, announce)
    await timers.async_load()
    recorder = ToolRecorder(hass, entry, partial(async_call_tool, hass, language, timers))
    executor = CodieExecutor(
        client,
        integration_id,
        recorder.async_handle,
        on_auth_failed=lambda: entry.async_start_reauth(hass),
        on_call=recorder.record,
    )
    settings = CodieSettingsCoordinator(hass, entry, client)
    # The settings do not gate setup: the Character works without them. If they cannot be read
    # now the entities start unavailable and recover on a later poll. A Codie that rejects
    # our credentials still starts re-authentication.
    await settings.async_refresh()
    entry.runtime_data = CodieRuntimeData(client=client, executor=executor, settings=settings)

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id)},
        manufacturer="CODE27",
        model="Codie",
        name=entry.title,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    cancel_retry: CALLBACK_TYPE | None = None

    async def refresh_catalog() -> None:
        """Re-register after the Assist tools changed, then reconnect to pick it up."""
        nonlocal cancel_retry
        try:
            if await registrar.async_refresh():
                _LOGGER.info("The available Assist tools changed; reconnecting the executor")
                await executor.async_reconnect()
        except CodieAuthError:
            entry.async_start_reauth(hass)
        except CodieError as err:
            # The next loaded component tries again, or the retry if none does: otherwise a
            # catalog the Codie refused would stay refused.
            _LOGGER.warning("Could not refresh the tools on the Codie: %s", err)
            # A refresh still running at unload must not leave a timer behind.
            if cancel_retry is None and not unloaded:
                cancel_retry = async_call_later(hass, REFRESH_RETRY_S, _retry_refresh)

    unloaded = False

    @callback
    def _retry_refresh(_now: Any) -> None:
        nonlocal cancel_retry
        cancel_retry = None
        debouncer.async_schedule_call()

    @callback
    def _cancel_retry() -> None:
        nonlocal unloaded
        unloaded = True
        if cancel_retry is not None:
            cancel_retry()

    entry.async_on_unload(_cancel_retry)

    # An integration that loads after this entry, or an upgrade, adds or removes Assist tools.
    debouncer = Debouncer(
        hass, _LOGGER, cooldown=REFRESH_COOLDOWN_S, immediate=False, function=refresh_catalog
    )
    entry.async_on_unload(debouncer.async_shutdown)

    # A callback, so it runs on the event loop rather than in an executor thread.
    @callback
    def _component_loaded(_event: Event) -> None:
        debouncer.async_schedule_call()

    entry.async_on_unload(hass.bus.async_listen(EVENT_COMPONENT_LOADED, _component_loaded))

    # turn_on lists the exposed scripts, so a script that comes, goes or is renamed, or whose
    # Expose setting changes, changes the catalog.
    @callback
    def _script_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["entity_id"].startswith("script."):
            debouncer.async_schedule_call()

    @callback
    def _expose_changed() -> None:
        debouncer.async_schedule_call()

    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _script_changed))
    entry.async_on_unload(async_listen_entity_updates(hass, "conversation", _expose_changed))
    entry.async_on_unload(executor.async_close)
    entry.async_on_unload(timers.async_unload)
    entry.async_create_background_task(hass, executor.async_run(), "codie executor")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Delete the Codie's Timers, and its tool integration from the Codie."""
    await async_remove_timers(hass, entry.entry_id)
    try:
        await _client_for(hass, entry).async_delete_tool_integration(
            entry.data[CONF_INTEGRATION_ID]
        )
    except CodieError as err:
        _LOGGER.warning("Could not delete the tool integration from the Codie: %s", err)
