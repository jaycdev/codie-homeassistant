"""Reconnecting, concurrency, lost calls and catalog refresh against the fake Codie."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component

from custom_components.codie.executor import CodieExecutor

from .fake_codie import FakeCodie
from .test_tools import INTEGRATION_ID, expose_light, setup, wait_connected


@pytest.fixture(autouse=True)
def fast_timers() -> Generator[None]:
    with (
        patch("custom_components.codie.executor.INITIAL_BACKOFF_S", 0.05),
        patch("custom_components.codie.executor.MAX_BACKOFF_S", 0.2),
        patch("custom_components.codie.REFRESH_COOLDOWN_S", 0.05),
    ):
        yield


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    """Assist reads the Expose list from the `homeassistant` component, always loaded for real."""
    assert await async_setup_component(hass, "homeassistant", {})


async def wait_for(condition: Callable[[], bool]) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


async def test_reconnects_after_the_socket_drops(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await expose_light(hass, "lamp", True)
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    await fake_codie.drop_executor()
    await wait_for(lambda: fake_codie.executor_connects == 2)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(INTEGRATION_ID, "get_home_state", {})
    assert result["status"] == "succeeded"


async def test_sensor_is_off_while_reconnecting(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    fake_codie.refuse_executor = True
    await fake_codie.drop_executor()
    await wait_connected(hass, fake_codie, False)
    await asyncio.sleep(0.3)  # several refused attempts
    state = hass.states.get("binary_sensor.codie_connected")
    assert state is not None
    assert state.state == "off"

    fake_codie.refuse_executor = False
    await wait_connected(hass, fake_codie, True)


async def test_backoff_doubles_up_to_the_maximum(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    delays: list[float] = []

    async def record(self: CodieExecutor, delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0.01)

    with patch.object(CodieExecutor, "_backoff", record):
        await setup(hass, fake_codie)
        await wait_connected(hass, fake_codie, True)
        fake_codie.refuse_executor = True
        await fake_codie.drop_executor()
        await wait_for(lambda: len(delays) >= 5)
        fake_codie.refuse_executor = False

    assert delays[:5] == [0.05, 0.1, 0.2, 0.2, 0.2]


async def test_three_invokes_run_concurrently(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    arrived = 0
    all_here = asyncio.Event()

    async def rendezvous(*args: Any) -> dict[str, Any]:
        nonlocal arrived
        arrived += 1
        if arrived == 3:
            all_here.set()
        await all_here.wait()
        return {"success": True}

    with patch("custom_components.codie.async_call_tool", rendezvous):
        await setup(hass, fake_codie)
        await wait_connected(hass, fake_codie, True)
        results = await asyncio.gather(
            *(fake_codie.invoke(INTEGRATION_ID, "get_home_state", {}) for _ in range(3))
        )

    assert [r["status"] for r in results] == ["succeeded"] * 3


async def test_calls_lost_at_disconnect_are_logged_and_not_retried(
    hass: HomeAssistant, fake_codie: FakeCodie, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    async def hang(*args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(30)
        return {}

    with patch("custom_components.codie.async_call_tool", hang):
        await setup(hass, fake_codie)
        await wait_connected(hass, fake_codie, True)
        pending = asyncio.create_task(fake_codie.invoke(INTEGRATION_ID, "get_home_state", {}))
        await wait_for(lambda: calls == 1)
        with caplog.at_level(logging.WARNING):
            await fake_codie.drop_executor()
            await wait_for(lambda: fake_codie.executor_connects == 2)
        pending.cancel()
        await asyncio.sleep(0.2)

    invocation_id = fake_codie.invocation_ids[0]
    assert any(
        invocation_id in r.getMessage() and "unknown" in r.getMessage() for r in caplog.records
    )
    assert calls == 1
    assert fake_codie.executor_results == []


async def test_protocol_error_without_invocation_reconnects(
    hass: HomeAssistant, fake_codie: FakeCodie, caplog: pytest.LogCaptureFixture
) -> None:
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    with caplog.at_level(logging.WARNING):
        await fake_codie.send_executor_error("SOME_CODE")
        await wait_for(lambda: fake_codie.executor_connects == 2)

    assert "SOME_CODE" in caplog.text
    await wait_connected(hass, fake_codie, True)


async def test_protocol_error_for_one_invocation_keeps_the_connection(
    hass: HomeAssistant, fake_codie: FakeCodie, caplog: pytest.LogCaptureFixture
) -> None:
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    with caplog.at_level(logging.WARNING):
        await fake_codie.send_executor_error("RESULT_REJECTED", invocation_id="abc")
        await asyncio.sleep(0.3)

    assert "RESULT_REJECTED" in caplog.text
    assert fake_codie.executor_connects == 1


async def test_new_assist_tool_triggers_reregistration_and_reconnect(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)
    catalog = fake_codie.tool_integrations[INTEGRATION_ID]["catalog"]
    before = {t["name"] for t in catalog["tools"]}
    assert "turn_on" not in before

    class TurnOn(intent.IntentHandler):
        intent_type = "HassTurnOn"

        async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
            return intent_obj.create_response()

    intent.async_register(hass, TurnOn())
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "light"})
    await wait_for(lambda: fake_codie.executor_connects == 2)
    await wait_connected(hass, fake_codie, True)

    stored = fake_codie.tool_integrations[INTEGRATION_ID]
    assert stored["revision"] == 2
    assert stored["catalog"]["expected_revision"] == 1
    assert {t["name"] for t in stored["catalog"]["tools"]} == before | {"turn_on"}


async def test_unchanged_tool_set_is_not_reregistered(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "light"})
    await asyncio.sleep(0.4)

    assert fake_codie.tool_integrations[INTEGRATION_ID]["revision"] == 1
    assert fake_codie.executor_connects == 1
