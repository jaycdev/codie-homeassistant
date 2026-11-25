"""The diagnostics download: what a bug report needs, without the credentials."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import CodieConfigEntry
from .api import CodieError
from .const import CONF_LOCAL_SECRET, CONF_LOCAL_SESSION_ID, CONF_USER_ID
from .settings import CodieSettings

# The credentials, and what identifies the Codie or its user. The Codie's system details may
# carry more than it reports today, so likely names for such identifiers are redacted too.
TO_REDACT = {
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
    "unique_id",
    "machine_id",
    "device_id",
    "username",
    "email",
    "serial",
    "serial_number",
    "mac",
    "mac_address",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CodieConfigEntry
) -> dict[str, Any]:
    """The entry, the executor, the Tool catalog, the settings, the Timers and the Codie."""
    runtime = entry.runtime_data
    executor = runtime.executor
    catalog = runtime.registrar.catalog
    settings = runtime.settings

    # Read now rather than at setup, so it shows the Codie as it is when the report is made.
    try:
        system: dict[str, Any] = await runtime.client.async_get_system()
    except CodieError as err:
        system = {"error": str(err)}

    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "executor": {
                "connected": executor.connected,
                "last_connected": executor.last_connected,
                "last_disconnect_reason": executor.last_disconnect_reason,
            },
            "catalog": None
            if catalog is None
            else {
                "revision": catalog.revision,
                "tools": {tool["name"]: tool["version"] for tool in catalog.tools},
                "left_out": {name: left.reason for name, left in catalog.left_out.items()},
            },
            "settings": {
                "last_update_success": settings.last_update_success,
                "last_exception": None
                if settings.last_exception is None
                else str(settings.last_exception),
                "data": None if settings.data is None else _settings_data(settings.data),
            },
            "timers": [
                {"name": name, "command": command, "ends_at": ends_at}
                for name, command, ends_at in runtime.timers.running
            ],
            "system": system,
        },
        TO_REDACT,
    )


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: CodieConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """The same as the entry's: an entry has exactly one Codie."""
    return await async_get_config_entry_diagnostics(hass, entry)


def _settings_data(data: CodieSettings) -> dict[str, Any]:
    """The settings as JSON: Slot numbers become strings, and the groups a sorted list."""
    values = asdict(data)
    values["characters"] = {str(slot): name for slot, name in sorted(data.characters.items())}
    values["unavailable"] = sorted(data.unavailable)
    return values
