"""The CODE27 Codie integration."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_listen_entity_updates
from homeassistant.components.intent import (
    TimerEventType,
    TimerInfo,
    async_register_timer_handler,
)
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_COMPONENT_LOADED, Platform
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .announce import async_announce, async_register_services
from .api import CodieApiError, CodieAuthError, CodieClient, CodieCredentials, CodieError
from .const import (
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
    DOMAIN,
)
from .executor import CodieExecutor
from .issues import CodieIssues, async_clear_issues, is_refusal
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

# HACS installs only this folder, so the blueprints ship inside it, one folder per blueprint domain.
BLUEPRINTS = Path(__file__).parent / "blueprints"

# Components load in bursts at startup; wait for the burst to end before comparing catalogs.
REFRESH_COOLDOWN_S = 10.0
# A refresh that failed is tried again after this, in case nothing else changes meanwhile.
REFRESH_RETRY_S = 300.0


@dataclass
class CodieRuntimeData:
    """What a loaded entry keeps in memory."""

    client: CodieClient
    registrar: CatalogRegistrar
    executor: CodieExecutor
    settings: CodieSettingsCoordinator
    timers: CodieTimers


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
    """Register the integration's actions and install its blueprints."""
    async_register_services(hass)
    await hass.async_add_executor_job(_install_blueprints, Path(hass.config.path("blueprints")))

    # Home Assistant does not call `async_unload_entry` for an entry whose setup failed, so
    # its repair issues are cleared here once it is disabled or stops retrying.
    @callback
    def _entry_changed(change: ConfigEntryChange, entry: ConfigEntry[Any]) -> None:
        if (
            entry.domain == DOMAIN
            and change is ConfigEntryChange.UPDATED
            and (entry.disabled_by is not None or entry.state is ConfigEntryState.NOT_LOADED)
        ):
            async_clear_issues(hass, entry.entry_id)

    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed)
    return True


def _install_blueprints(config_blueprints: Path) -> None:
    """Copy each shipped blueprint that is missing; one already there may hold the user's edits."""
    for domain_folder in BLUEPRINTS.iterdir():
        target = config_blueprints / domain_folder.name / DOMAIN
        for blueprint in domain_folder.glob("*.yaml"):
            if not (target / blueprint.name).exists():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(blueprint, target / blueprint.name)


async def async_setup_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Set up a Codie from a config entry."""
    if entry.unique_id is None:
        raise ConfigEntryError("The Codie entry has no unique ID; remove and add it again")
    # The issues outlive a setup that fails, so a Codie that keeps refusing us still shows why.
    # They are cleared once it works, and when the entry is unloaded, disabled or removed.
    issues = CodieIssues(hass, entry)
    client = _client_for(hass, entry, issues.clock_skew)
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

    registrar = CatalogRegistrar(hass, client, integration_id, language, issues)
    try:
        await registrar.async_register()
    except CodieAuthError as err:
        raise ConfigEntryAuthFailed(f"The Codie rejected our credentials: {err}") from err
    except CodieError as err:
        raise ConfigEntryNotReady(f"Cannot register tools on the Codie: {err}") from err

    async def announce(message: str) -> None:
        await async_announce(client, message, partial(entry.async_start_reauth, hass))

    # The Tools and Timers pass the device's id to Assist, so a request or Command that names no
    # Area acts on the Codie's.
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id)},
        manufacturer="CODE27",
        model="Codie",
        name=entry.title,
    )
    timers = CodieTimers(hass, timers_store(hass, entry.entry_id), language, device.id, announce)
    await timers.async_load()
    # Right away: a later step of this setup may fail, and a retry loads the store again.
    entry.async_on_unload(timers.async_unload)

    # Home Assistant's own timers that something else starts on the Codie's device ring on the
    # Codie. Without a handler they fail with TimersNotSupportedError.
    @callback
    def _timer_event(event_type: TimerEventType, info: TimerInfo) -> None:
        if event_type is TimerEventType.FINISHED:
            entry.async_create_task(hass, timers.async_announce_done(info.name), "codie timer done")

    try:
        entry.async_on_unload(async_register_timer_handler(hass, device.id, _timer_event))
    except KeyError:
        # Home Assistant's intent integration is not set up, so it has no timers to ring.
        _LOGGER.debug("Home Assistant has no timers; not registering the Codie as a timer device")
    recorder = ToolRecorder(
        hass, entry, partial(async_call_tool, hass, language, device.id, timers)
    )
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
    entry.runtime_data = CodieRuntimeData(
        client=client, registrar=registrar, executor=executor, settings=settings, timers=timers
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    cancel_retry: CALLBACK_TYPE | None = None
    # Whether the last refresh failed, so a long outage is logged once rather than each retry.
    refresh_failing = False
    # Set once this setup unloads. A refresh can outlive it, and the entry's state may already
    # belong to the next setup.
    unloaded = False

    @callback
    def _cancel_retry() -> None:
        nonlocal cancel_retry
        if cancel_retry is not None:
            cancel_retry()
            cancel_retry = None

    @callback
    def _on_unload() -> None:
        nonlocal unloaded
        unloaded = True
        _cancel_retry()

    async def refresh_catalog() -> None:
        """Re-register after the Assist tools changed, then reconnect to pick it up."""
        nonlocal cancel_retry, refresh_failing
        _cancel_retry()
        try:
            if await registrar.async_refresh():
                _LOGGER.info("The available Assist tools changed; reconnecting the executor")
                await executor.async_reconnect()
        except CodieAuthError:
            entry.async_start_reauth(hass)
        except CodieError as err:
            log = _LOGGER.debug if refresh_failing else _LOGGER.warning
            log("Could not refresh the tools on the Codie: %s", err)
            refresh_failing = True
            # The next loaded component tries again, or the retry if none loads. A refused
            # catalog would be refused again; its repair issue tells the user instead.
            if not unloaded and not (isinstance(err, CodieApiError) and is_refusal(err)):
                cancel_retry = async_call_later(hass, REFRESH_RETRY_S, _retry_refresh)
        else:
            if refresh_failing:
                _LOGGER.info("The tools on the Codie are up to date again")
            refresh_failing = False

    # An integration that loads after this entry, or an upgrade, adds or removes Assist tools.
    debouncer = Debouncer(
        hass, _LOGGER, cooldown=REFRESH_COOLDOWN_S, immediate=False, function=refresh_catalog
    )
    entry.async_on_unload(debouncer.async_shutdown)
    entry.async_on_unload(_on_unload)

    @callback
    def _retry_refresh(_now: datetime) -> None:
        nonlocal cancel_retry
        cancel_retry = None
        debouncer.async_schedule_call()

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
    if hass.state is not CoreState.running:
        # Tools left out are only reported once Home Assistant has started.
        @callback
        def _started(_hass: HomeAssistant) -> None:
            debouncer.async_schedule_call()

        entry.async_on_unload(async_at_started(hass, _started))
    entry.async_on_unload(executor.async_close)
    entry.async_create_background_task(hass, executor.async_run(), "codie executor")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CodieConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_clear_issues(hass, entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Delete the Codie's Timers and repair issues, and its tool integration from the Codie."""
    async_clear_issues(hass, entry.entry_id)
    await async_remove_timers(hass, entry.entry_id)
    try:
        await _client_for(hass, entry).async_delete_tool_integration(
            entry.data[CONF_INTEGRATION_ID]
        )
    except CodieError as err:
        _LOGGER.warning("Could not delete the tool integration from the Codie: %s", err)
