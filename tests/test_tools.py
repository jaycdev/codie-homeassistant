"""Registration, the executor connection, and `get_home_state` against the fake Codie."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codie.const import (
    CONF_INTEGRATION_ID,
    CONF_LOCAL_SECRET,
    CONF_USER_ID,
    DOMAIN,
)

from .fake_codie import FakeCodie

INTEGRATION_ID = "7b4a7f6e-0000-4000-8000-000000000000"


def make_entry(fake: FakeCodie) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Codie",
        unique_id=fake.machine_id,
        data={
            CONF_HOST: fake.host,
            CONF_PORT: fake.port,
            CONF_LOCAL_SECRET: fake.local_secret,
            CONF_USER_ID: fake.user_id,
            CONF_INTEGRATION_ID: INTEGRATION_ID,
        },
    )


async def setup(hass: HomeAssistant, fake: FakeCodie) -> MockConfigEntry:
    entry = make_entry(fake)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def wait_connected(hass: HomeAssistant, fake: FakeCodie, connected: bool) -> None:
    for _ in range(100):
        state = hass.states.get("binary_sensor.codie_connected")
        if (
            state is not None
            and (state.state == "on") is connected
            and (fake.executor_connected is connected)
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached connected={connected}")


async def expose_light(hass: HomeAssistant, name: str, exposed: bool) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    entry = er.async_get(hass).async_get_or_create("light", "test", name, suggested_object_id=name)
    hass.states.async_set(entry.entity_id, "on", {"friendly_name": name})
    async_expose_entity(hass, "conversation", entry.entity_id, exposed)


async def test_registers_catalog_with_get_home_state(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)

    stored = fake_codie.tool_integrations[INTEGRATION_ID]
    assert stored["revision"] == 1
    catalog = stored["catalog"]
    assert catalog["protocol_version"] == "v1"
    assert catalog["expected_revision"] == 0
    tool = next(t for t in catalog["tools"] if t["name"] == "get_home_state")
    assert tool["risk"] == "read"
    assert tool["confirmation_required"] is False
    assert tool["parameters"]["type"] == "object"
    assert tool["timeout_ms"] <= 20000


async def test_update_uses_current_revision(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    # Someone else already registered this id, so a create at revision 0 conflicts.
    fake_codie.tool_integrations[INTEGRATION_ID] = {"revision": 4, "catalog": {"tools": []}}

    await setup(hass, fake_codie)

    assert fake_codie.tool_integrations[INTEGRATION_ID]["revision"] == 5
    puts = [r for r in fake_codie.requests if r.method == "PUT"]
    assert [p.json["expected_revision"] for p in puts][-1] == 4


async def test_registration_rereads_and_retries_on_conflict(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.tool_integrations[INTEGRATION_ID] = {"revision": 1, "catalog": {"tools": []}}
    fake_codie.put_conflicts = 1

    await setup(hass, fake_codie)

    assert fake_codie.tool_integrations[INTEGRATION_ID]["revision"] == 3
    assert fake_codie.tool_integrations[INTEGRATION_ID]["catalog"]["tools"]


async def test_invoke_returns_only_exposed_entities(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await expose_light(hass, "kitchen_lamp", True)
    await expose_light(hass, "secret_lamp", False)
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(INTEGRATION_ID, "get_home_state", {})

    assert result["status"] == "succeeded"
    data: dict[str, Any] = result["data"]
    assert data["success"] is True
    assert "kitchen_lamp" in data["result"]
    assert "secret_lamp" not in data["result"]
    schema = fake_codie.tool_integrations[INTEGRATION_ID]["catalog"]["tools"][0]["result_schema"]
    assert set(data) <= set(schema["properties"])
    assert set(schema["required"]) <= set(data)


async def _language_of_call(hass: HomeAssistant, fake: FakeCodie) -> str | None:
    """The language of the LLM context a Tool call runs with."""
    await expose_light(hass, "kitchen_lamp", True)
    await setup(hass, fake)
    await wait_connected(hass, fake, True)
    with patch("custom_components.codie.tools.llm.async_get_api", wraps=llm.async_get_api) as spy:
        result = await fake.invoke(INTEGRATION_ID, "get_home_state", {})
    assert result["status"] == "succeeded"
    language: str | None = spy.call_args.args[2].language
    return language


async def test_call_runs_in_the_ai_language(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    assert await _language_of_call(hass, fake_codie) == "ja"


async def test_call_has_no_language_without_ai_language(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    async def system() -> dict[str, Any]:
        return {"language": "en"}

    with patch("custom_components.codie.api.CodieClient.async_get_system", side_effect=system):
        assert await _language_of_call(hass, fake_codie) is None


async def test_unknown_tool_fails(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(INTEGRATION_ID, "nope", {})

    assert result["status"] == "failed"
    assert result["code"] == "unknown_tool"


async def test_connected_sensor_follows_connection(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    await fake_codie.drop_executor()
    await wait_connected(hass, fake_codie, False)


async def test_unload_closes_socket(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not fake_codie.executor_connected
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_removing_entry_deletes_tool_integration(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)

    assert await hass.config_entries.async_remove(entry.entry_id)

    assert fake_codie.deleted_integrations == [INTEGRATION_ID]
    assert INTEGRATION_ID not in fake_codie.tool_integrations


async def test_slow_call_fails_before_the_deadline(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    async def slow(*args: Any) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    with patch("custom_components.codie.async_call_tool", slow):
        await setup(hass, fake_codie)
        await wait_connected(hass, fake_codie, True)
        result = await fake_codie.invoke(INTEGRATION_ID, "get_home_state", {}, timeout_ms=800)

    assert result["status"] == "failed"
    assert result["code"] == "timeout"


def test_result_is_cut_by_bytes_not_characters() -> None:
    from custom_components.codie.tools import MAX_RESULT_BYTES, _shape_result

    data = _shape_result({"success": True, "result": "あ" * MAX_RESULT_BYTES})

    assert len(data["result"].encode()) <= MAX_RESULT_BYTES
