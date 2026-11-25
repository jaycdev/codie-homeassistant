"""The diagnostics download, against the fake Codie."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.codie.const import CONF_LOCAL_SESSION_ID, DOMAIN
from custom_components.codie.schema import SchemaLimitError, from_probatio
from custom_components.codie.tools import TOOLS

from .fake_codie import FakeCodie
from .test_resilience import wait_for
from .test_tool_list import add_entity, setup_assist
from .test_tools import INTEGRATION_ID, expose_light, make_entry, setup, wait_connected

REDACTED = "**REDACTED**"
# What the fake Codie answers a signed request with while it rejects our credentials.
REJECTED = "HTTP 401 LOCAL_SECRET_MISMATCH: LOCAL_SECRET_MISMATCH"
REFUSED = "CodieConnectionError: The Codie refused the executor: 503"

Cause = Callable[[HomeAssistant, FakeCodie, MockConfigEntry], Awaitable[None]]


async def wait_for_reason(entry: MockConfigEntry, reason: str) -> None:
    await wait_for(lambda: entry.runtime_data.executor.last_disconnect_reason == reason)


async def _drop(hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry) -> None:
    await fake.drop_executor()


async def _refuse(hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry) -> None:
    fake.refuse_executor = True
    await fake.drop_executor()


async def _reject(hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry) -> None:
    fake.auth_error = "LOCAL_SECRET_MISMATCH"
    await fake.drop_executor()


async def _protocol_error(hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry) -> None:
    await fake.send_executor_error("SOME_CODE")


async def _reconnect(hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry) -> None:
    await entry.runtime_data.executor.async_reconnect()


async def test_reports_the_connection_catalog_settings_and_system(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_codie: FakeCodie
) -> None:
    await expose_light(hass, "lamp", True)
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"] == {
        "title": "Codie",
        "unique_id": REDACTED,
        "data": {
            "host": fake_codie.host,
            "port": fake_codie.port,
            "local_secret": REDACTED,
            "user_id": REDACTED,
            "integration_id": INTEGRATION_ID,
        },
        "options": {},
    }
    executor = diagnostics["executor"]
    assert executor["connected"] is True
    assert executor["last_connected"] is not None
    assert executor["last_disconnect_reason"] is None

    catalog = diagnostics["catalog"]
    registered = fake_codie.tool_integrations[INTEGRATION_ID]
    assert catalog["revision"] == registered["revision"]
    assert catalog["tools"] == {
        tool["name"]: tool["version"] for tool in registered["catalog"]["tools"]
    }
    # The intent integration is not loaded, so the Tools that run an intent are left out, and
    # the report says why. Every Tool is either registered or left out.
    assert catalog["left_out"]["turn_on"] == "Assist tool HassTurnOn is not available"
    assert set(catalog["tools"]) | set(catalog["left_out"]) == {tool.name for tool in TOOLS}
    assert not set(catalog["tools"]) & set(catalog["left_out"])

    settings = diagnostics["settings"]
    assert settings["last_update_success"] is True
    assert settings["data"]["slot"] == 3
    assert settings["data"]["characters"]["3"] == "Cleo"
    assert settings["data"]["unavailable"] == []

    system = diagnostics["system"]
    assert system["machine_id"] == REDACTED
    assert system["user_id"] == REDACTED
    assert system["username"] == REDACTED
    assert system["ai_language"] == "ja"


# Closing a live socket can leave aiohttp's heartbeat timer behind; see test_resilience.
@pytest.mark.parametrize(
    ("cause", "reason", "expected_lingering_timers"),
    [
        (_drop, "The Codie closed the connection (code 1000)", True),
        (_refuse, REFUSED, True),
        (_reject, f"The Codie rejected our credentials: {REJECTED}", True),
        (_protocol_error, "The Codie reported a protocol error", True),
        (_reconnect, "The Tool catalog changed", True),
    ],
    ids=["closed", "refused", "rejected", "protocol_error", "reconnect"],
)
async def test_reports_why_the_executor_disconnected(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_codie: FakeCodie,
    cause: Cause,
    reason: str,
) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    await cause(hass, fake_codie, entry)
    await wait_for_reason(entry, reason)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["executor"]["last_disconnect_reason"] == reason


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_reports_a_heartbeat_that_got_no_answer(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_codie: FakeCodie
) -> None:
    fake_codie.answer_pings = False
    with patch("custom_components.codie.api.EXECUTOR_HEARTBEAT_S", 0.2):
        entry = await setup(hass, fake_codie)
        await wait_for(
            lambda: (entry.runtime_data.executor.last_disconnect_reason or "").startswith(
                "The connection failed: ServerTimeoutError: "
            )
        )
        fake_codie.answer_pings = True

        diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert "PONG" in diagnostics["executor"]["last_disconnect_reason"]


async def test_reports_a_codie_that_cannot_give_its_system_details(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)
    fake_codie.overrides["/data/user/system"] = (
        500,
        b'{"success": false, "code": "INTERNAL", "message": "boom"}',
    )

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["system"] == {"error": "HTTP 500 INTERNAL: boom"}
    assert diagnostics["entry"]["data"]["integration_id"] == INTEGRATION_ID
    assert diagnostics["executor"]["connected"] is True
    assert diagnostics["settings"]["data"]["slot"] == 3
    assert diagnostics["catalog"] is not None


async def test_reports_a_tool_left_out_for_its_schema(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    registrar = entry.runtime_data.registrar
    assert registrar.catalog is not None
    tools = registrar.catalog.tools
    assert registrar.catalog.left_out["turn_on"].reason == "Assist tool HassTurnOn is not available"

    # HassTurnOn arrives, but with a parameter the Codie cannot take, so turn_on stays out:
    # the Tools are unchanged and only the reason differs.
    class TurnOn(intent.IntentHandler):
        intent_type = "HassTurnOn"
        slot_schema: ClassVar[dict[Any, Any]] = {vol.Optional("too_big"): str}

        async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
            return intent_obj.create_response()

    def limited(parameters: Any, serializer: Any) -> dict[str, Any]:
        if "too_big" in str(parameters.schema):
            raise SchemaLimitError("too many properties")
        return from_probatio(parameters, serializer)

    intent.async_register(hass, TurnOn())
    with patch("custom_components.codie.tools.from_probatio", side_effect=limited):
        assert not await registrar.async_refresh()

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["catalog"]["left_out"]["turn_on"] == "too many properties"
    assert registrar.catalog.tools == tools


async def test_reports_the_running_timers(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_codie: FakeCodie
) -> None:
    # The built-in agent knows English, so the Timer Tools are offered.
    fake_codie.ai_language = "en"
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen lamp", True)
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)
    result = await fake_codie.invoke(
        INTEGRATION_ID, "timer_start", {"command": "turn off the kitchen lamp", "minutes": 20}
    )
    assert result["status"] == "succeeded"

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    (timer,) = diagnostics["timers"]
    assert timer["name"] == timer["command"] == "turn off the kitchen lamp"
    assert timer["ends_at"] is not None
    assert "timer_start" in diagnostics["catalog"]["tools"]


async def _connected(hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry) -> None:
    await wait_connected(hass, fake, True)


async def _refused_and_unreadable(
    hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry
) -> None:
    fake.overrides["/data/user/system"] = (500, b'{"success": false, "code": "INTERNAL"}')
    await _refuse(hass, fake, entry)
    await wait_for_reason(entry, REFUSED)


async def _rejected(hass: HomeAssistant, fake: FakeCodie, entry: MockConfigEntry) -> None:
    await _reject(hass, fake, entry)
    await wait_for_reason(entry, f"The Codie rejected our credentials: {REJECTED}")


@pytest.mark.parametrize(
    ("state", "expected_lingering_timers"),
    [(_connected, False), (_refused_and_unreadable, True), (_rejected, True)],
    ids=["connected", "refused_and_unreadable", "rejected"],
)
async def test_no_secret_appears_in_either_download(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_codie: FakeCodie, state: Cause
) -> None:
    fake_codie.local_session_id = "fake-local-session-id"
    base = make_entry(fake_codie)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=base.title,
        unique_id=base.unique_id,
        data={**base.data, CONF_LOCAL_SESSION_ID: fake_codie.local_session_id},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_connected(hass, fake_codie, True)
    await state(hass, fake_codie, entry)
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)

    downloads = [
        await get_diagnostics_for_config_entry(hass, hass_client, entry),
        await get_diagnostics_for_device(hass, hass_client, entry, device),
    ]

    secrets = (
        fake_codie.local_secret,
        fake_codie.local_session_id,
        fake_codie.user_id,
        fake_codie.machine_id,
        fake_codie.username,
    )
    for download in downloads:
        text = json.dumps(download)
        for secret in secrets:
            assert secret not in text
