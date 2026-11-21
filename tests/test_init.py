"""Setting up and unloading a Codie entry."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codie.const import (
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_USER_ID,
    DOMAIN,
)


async def test_entry_loads_and_unloads(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a" * 64,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 8081,
            CONF_LOCAL_SECRET: "secret",
            CONF_USER_ID: "user",
            CONF_INTEGRATION_ID: "7b4a7f6e-0000-4000-8000-000000000000",
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    state: ConfigEntryState = entry.state
    assert state is ConfigEntryState.NOT_LOADED
