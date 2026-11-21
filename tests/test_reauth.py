"""Re-authentication when the credentials stop working, against the fake Codie."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codie.const import (
    CONF_CODE,
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
)

from .fake_codie import FakeCodie
from .test_tools import INTEGRATION_ID, expose_light, make_entry, setup, wait_connected

REAUTH_CODES = [
    "LOCAL_USER_MISMATCH",
    "LOCAL_SECRET_MISMATCH",
    "INVALID_SIGNATURE",
    "LOCAL_SESSION_EXPIRED",
]


@pytest.fixture(autouse=True)
def fast_timers() -> Generator[None]:
    with (
        patch("custom_components.codie.executor.INITIAL_BACKOFF_S", 0.05),
        patch("custom_components.codie.executor.MAX_BACKOFF_S", 0.2),
    ):
        yield


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    """Assist reads the Expose list from the `homeassistant` component."""
    assert await async_setup_component(hass, "homeassistant", {})


def reauth_flows(hass: HomeAssistant, entry: MockConfigEntry) -> list[Any]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == config_entries.SOURCE_REAUTH
        and flow["context"].get("entry_id") == entry.entry_id
    ]


async def wait_for_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    for _ in range(100):
        if reauth_flows(hass, entry):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("reauth never started")


async def break_credentials(hass: HomeAssistant, fake: FakeCodie) -> MockConfigEntry:
    """Add an entry whose stored secret is stale, and let setup fail on it."""
    entry = make_entry(fake)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_LOCAL_SECRET: "stale"})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def confirm_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Submit the reauth confirmation; returns the result (the pair step if all went well)."""
    (flow,) = reauth_flows(hass, entry)
    assert flow["step_id"] == "reauth_confirm"
    return await hass.config_entries.flow.async_configure(flow["flow_id"], {})


@pytest.mark.parametrize("code", REAUTH_CODES)
async def test_rejected_credentials_at_setup_start_reauth(
    hass: HomeAssistant, fake_codie: FakeCodie, code: str
) -> None:
    fake_codie.auth_error = code
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(reauth_flows(hass, entry)) == 1


@pytest.mark.parametrize("code", REAUTH_CODES)
async def test_rejected_credentials_while_running_start_reauth(
    hass: HomeAssistant, fake_codie: FakeCodie, code: str
) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    # The Codie restarts with other credentials; our reconnect is turned away.
    fake_codie.auth_error = code
    await fake_codie.drop_executor()
    await wait_for_reauth(hass, entry)
    await wait_connected(hass, fake_codie, False)

    # It stops reconnecting with credentials that cannot work.
    connects = fake_codie.executor_connects
    await asyncio.sleep(0.3)
    assert fake_codie.executor_connects == connects


async def test_clock_skew_logs_and_does_not_start_reauth(
    hass: HomeAssistant, fake_codie: FakeCodie, caplog: pytest.LogCaptureFixture
) -> None:
    fake_codie.auth_error = "REQUEST_EXPIRED"
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    with caplog.at_level(logging.WARNING):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert reauth_flows(hass, entry) == []
    assert "clock" in caplog.text.lower()


async def test_clock_skew_while_running_does_not_start_reauth(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    fake_codie.auth_error = "REQUEST_EXPIRED"
    await fake_codie.drop_executor()
    await asyncio.sleep(0.3)  # several refused attempts

    assert reauth_flows(hass, entry) == []
    fake_codie.auth_error = None
    await wait_connected(hass, fake_codie, True)


async def test_reauth_pairs_again_and_brings_the_tools_back(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await expose_light(hass, "lamp", True)
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    # A different user signs in on the Codie, which issues new credentials.
    fake_codie.user_id = "99999999-2222-3333-4444-555555555555"
    fake_codie.local_secret = "another-secret"
    fake_codie.local_session_id = "session-7"
    fake_codie.tool_integrations.clear()
    await fake_codie.drop_executor()
    await wait_for_reauth(hass, entry)

    result = await confirm_reauth(hass, entry)
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: fake_codie.pairing_code}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_LOCAL_SECRET] == "another-secret"
    assert entry.data[CONF_USER_ID] == fake_codie.user_id
    assert entry.data[CONF_LOCAL_SESSION_ID] == "session-7"
    # The same integration_id, so the Codie's registration is reused.
    assert entry.data[CONF_INTEGRATION_ID] == INTEGRATION_ID
    assert entry.state is ConfigEntryState.LOADED
    assert INTEGRATION_ID in fake_codie.tool_integrations
    await wait_connected(hass, fake_codie, True)
    invoked = await fake_codie.invoke(INTEGRATION_ID, "get_home_state", {})
    assert invoked["status"] == "succeeded"


async def test_reauth_drops_a_session_id_the_codie_no_longer_issues(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.local_session_id = "old-session"
    entry = await break_credentials(hass, fake_codie)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_LOCAL_SESSION_ID: "x"})
    fake_codie.local_session_id = ""
    assert reauth_flows(hass, entry)

    result = await confirm_reauth(hass, entry)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: fake_codie.pairing_code}
    )
    await hass.async_block_till_done()

    assert CONF_LOCAL_SESSION_ID not in entry.data
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_with_the_wrong_code_asks_again(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await break_credentials(hass, fake_codie)

    result = await confirm_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "000000"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_code"}


async def test_reauth_refuses_a_different_codie(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    entry = await break_credentials(hass, fake_codie)

    result = await confirm_reauth(hass, entry)
    fake_codie.machine_id = "b" * 64  # another device now answers at this address
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: fake_codie.pairing_code}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_LOCAL_SECRET] == "stale"


async def test_reauth_reports_a_codie_that_is_not_signed_in(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await break_credentials(hass, fake_codie)
    fake_codie.logged_in = False

    result = await confirm_reauth(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_signed_in"
