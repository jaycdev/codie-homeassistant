"""Shared fixtures."""

from collections.abc import AsyncGenerator

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .fake_codie import FakeCodie


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    """The Expose list lives in `homeassistant`, which is always loaded for real."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
async def fake_codie(socket_enabled: None) -> AsyncGenerator[FakeCodie]:
    codie = FakeCodie()
    await codie.start()
    yield codie
    await codie.stop()
