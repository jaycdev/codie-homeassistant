"""Shared fixtures."""

import sys
from collections.abc import AsyncGenerator

import pytest
import pytest_socket

from .fake_codie import FakeCodie

if sys.platform == "win32":
    # Home Assistant's test plugin bans sockets, but asyncio's event loop needs a loopback
    # socketpair on Windows, so nothing could run. Linux CI keeps the ban.
    pytest_socket.disable_socket = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/codie."""


@pytest.fixture
async def fake_codie(socket_enabled: None) -> AsyncGenerator[FakeCodie]:
    codie = FakeCodie()
    await codie.start()
    yield codie
    await codie.stop()
