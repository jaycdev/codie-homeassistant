"""Pairing through the config flow, against the fake Codie."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.codie.api import CodieConnectionError
from custom_components.codie.const import (
    CONF_CODE,
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_LOCAL_SESSION_ID,
    CONF_USER_ID,
    DEFAULT_PORT,
    DOMAIN,
)

from .fake_codie import FakeCodie


async def start_flow(hass: HomeAssistant, codie: FakeCodie) -> ConfigFlowResult:
    """Begin the flow and submit the host and port."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: codie.host, CONF_PORT: codie.port}
    )


async def enter_code(hass: HomeAssistant, flow_id: str, code: str) -> ConfigFlowResult:
    return await hass.config_entries.flow.async_configure(flow_id, {CONF_CODE: code})


async def test_form_defaults_to_port_8081(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["data_schema"] is not None
    (port_key,) = [key for key in result["data_schema"].schema if key == CONF_PORT]
    assert port_key.default() == DEFAULT_PORT


async def test_happy_path_creates_an_entry_and_a_device(
    hass: HomeAssistant, fake_codie: FakeCodie, device_registry: dr.DeviceRegistry
) -> None:
    result = await start_flow(hass, fake_codie)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert len(fake_codie.requests_to("/auth/pair/request")) == 1

    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == fake_codie.machine_id
    assert entry.data[CONF_HOST] == fake_codie.host
    assert entry.data[CONF_PORT] == fake_codie.port
    assert entry.data[CONF_LOCAL_SECRET] == fake_codie.local_secret
    assert entry.data[CONF_USER_ID] == fake_codie.user_id
    # An empty local_session_id is not stored.
    assert CONF_LOCAL_SESSION_ID not in entry.data
    integration_id = entry.data[CONF_INTEGRATION_ID]
    assert integration_id == str(uuid.UUID(integration_id))  # canonical lowercase UUID

    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert [device.identifiers for device in devices] == [{(DOMAIN, fake_codie.machine_id)}]
    # Pairing finished, so there is nothing left to cancel.
    assert fake_codie.cancelled_challenges == []


async def test_session_id_is_stored_when_the_codie_issues_one(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.local_session_id = "session-42"

    result = await start_flow(hass, fake_codie)
    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCAL_SESSION_ID] == "session-42"
    # The unique-ID lookup was signed with the session header and accepted.
    (system,) = fake_codie.requests_to("/data/user/system")
    assert system.rejected_with is None


async def test_session_scope_is_never_requested(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    result = await start_flow(hass, fake_codie)
    await enter_code(hass, result["flow_id"], "000000")  # wrong, then right
    await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert fake_codie.confirm_scopes == ["permanent", "permanent"]
    for request in fake_codie.requests_to("/auth/pair/confirm"):
        assert request.json["auth_scope"] == "permanent"


async def test_adding_the_same_codie_again_aborts(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)
    await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    result = await start_flow(hass, fake_codie)
    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_two_codies_get_separate_entries_and_integration_ids(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    other = FakeCodie(machine_id="b" * 64, local_secret="other-secret")
    await other.start()
    try:
        for codie in (fake_codie, other):
            result = await start_flow(hass, codie)
            result = await enter_code(hass, result["flow_id"], codie.pairing_code)
            assert result["type"] is FlowResultType.CREATE_ENTRY
    finally:
        await other.stop()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert entries[0].data[CONF_INTEGRATION_ID] != entries[1].data[CONF_INTEGRATION_ID]


async def test_wrong_code_shows_an_error_and_can_be_retried(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)

    for _ in range(2):
        result = await enter_code(hass, result["flow_id"], "000000")
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "pair"
        assert result["errors"] == {"base": "invalid_code"}

    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # One challenge throughout, never cancelled.
    assert len(fake_codie.requests_to("/auth/pair/request")) == 1
    assert fake_codie.cancelled_challenges == []


async def test_an_expired_code_aborts_and_cancels_the_challenge(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)
    challenge_id = fake_codie.challenge_id
    fake_codie.expire_challenge()

    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "code_expired"
    assert fake_codie.cancelled_challenges == [challenge_id]
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_abandoning_the_flow_cancels_the_challenge(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)
    challenge_id = fake_codie.challenge_id

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert fake_codie.cancelled_challenges == [challenge_id]


async def test_abandoning_before_a_challenge_exists_cancels_nothing(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert fake_codie.requests == []


async def test_unreachable_codie_shows_a_connection_error(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await fake_codie.stop()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_no_signed_in_user_aborts_without_requesting_a_code(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.logged_in = False

    result = await start_flow(hass, fake_codie)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_signed_in"
    assert fake_codie.requests_to("/auth/pair/request") == []


async def test_codie_going_away_after_the_code_aborts(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)
    await fake_codie.stop()

    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_not_logged_in_refusal_from_pair_request_aborts(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/auth/pair/request"] = (
        403,
        b'{"success":false,"code":"DEVICE_NOT_LOGGED_IN"}',
    )

    result = await start_flow(hass, fake_codie)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_signed_in"


async def test_bootstrap_without_a_user_id_counts_as_not_signed_in(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/auth/bootstrap"] = (
        200,
        b'{"success":true,"data":{"device_logged_in":true,"user_id":""}}',
    )

    result = await start_flow(hass, fake_codie)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_signed_in"


async def test_other_pair_request_failures_are_not_called_connection_errors(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/auth/pair/request"] = (
        429,
        b'{"success":false,"code":"PAIRING_REQUEST_RATE_LIMITED"}',
    )

    result = await start_flow(hass, fake_codie)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "unknown"}


async def test_system_reply_without_machine_id_aborts_as_unknown(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/data/user/system"] = (200, b'{"success":true,"data":{}}')
    result = await start_flow(hass, fake_codie)

    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


async def test_malformed_pair_request_reply_shows_an_unknown_error(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/auth/pair/request"] = (200, b"<html>oops</html>")

    result = await start_flow(hass, fake_codie)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_unexpected_confirm_failures_keep_the_form_open(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)

    fake_codie.overrides["/auth/pair/confirm"] = (500, b'{"success":false,"code":"BOOM"}')
    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

    fake_codie.overrides["/auth/pair/confirm"] = (200, b"not json")
    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

    del fake_codie.overrides["/auth/pair/confirm"]
    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_system_failure_after_confirm_aborts_as_unknown(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/data/user/system"] = (500, b'{"success":false,"code":"BOOM"}')
    result = await start_flow(hass, fake_codie)

    result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


async def test_failing_to_cancel_the_challenge_is_harmless(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)
    fake_codie.overrides["/auth/pair/cancel"] = (500, b"{}")

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert fake_codie.cancelled_challenges == []
    assert len(fake_codie.requests_to("/auth/pair/cancel")) == 1


async def test_connection_lost_after_confirm_aborts(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    result = await start_flow(hass, fake_codie)

    with patch(
        "custom_components.codie.config_flow.CodieClient.async_get_system",
        side_effect=CodieConnectionError,
    ):
        result = await enter_code(hass, result["flow_id"], fake_codie.pairing_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
