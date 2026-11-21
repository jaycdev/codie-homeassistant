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

from .fake_codie import FakeCodie


async def test_entry_loads_and_unloads(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a" * 64,
        data={
            CONF_HOST: fake_codie.host,
            CONF_PORT: fake_codie.port,
            CONF_LOCAL_SECRET: fake_codie.local_secret,
            CONF_USER_ID: fake_codie.user_id,
            CONF_INTEGRATION_ID: "7b4a7f6e-0000-4000-8000-000000000000",
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    state: ConfigEntryState = entry.state
    assert state is ConfigEntryState.NOT_LOADED
