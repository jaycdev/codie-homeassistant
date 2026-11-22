"""Shared fixtures."""

from collections.abc import AsyncGenerator

import pytest

from .fake_codie import FakeCodie


@pytest.fixture
async def fake_codie(socket_enabled: None) -> AsyncGenerator[FakeCodie]:
    codie = FakeCodie()
    await codie.start()
    yield codie
    await codie.stop()
