"""Repair issues for problems the user can fix, against the fake Codie."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.codie.const import DOMAIN

from .fake_codie import FakeCodie
from .test_resilience import CLOSES_A_LIVE_SOCKET
from .test_tool_list import FakeLiveContext, setup_assist
from .test_tools import INTEGRATION_ID, make_entry, setup, wait_connected

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "codie" / "translations"


@pytest.fixture(autouse=True)
def fast_timers() -> Generator[None]:
    with (
        patch("custom_components.codie.executor.INITIAL_BACKOFF_S", 0.05),
        patch("custom_components.codie.executor.MAX_BACKOFF_S", 0.2),
        patch("custom_components.codie.REFRESH_COOLDOWN_S", 0.05),
        patch("custom_components.codie.REFRESH_RETRY_S", 0.1),
    ):
        yield


@pytest.fixture(autouse=True)
async def core_loaded(hass: HomeAssistant) -> None:
    """Assist reads the Expose list from `homeassistant`, and its API comes from `llm`."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})


def issues(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> list[ir.IssueEntry]:
    """The entry's repair issues with translation `key`, checking each has its text."""
    found = [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
        and issue.config_entry_id == entry.entry_id
        and issue.translation_key == key
    ]
    strings = json.loads((TRANSLATIONS / "en.json").read_text("utf-8"))["issues"]
    for issue in found:
        assert issue.translation_key is not None
        assert {"title", "description"} <= strings[issue.translation_key].keys()
    return found


async def wait_for(condition: Callable[[], bool]) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


# --- clock skew --------------------------------------------------------------------------


async def test_clock_skew_at_setup_raises_an_issue_cleared_once_requests_work(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.auth_error = "REQUEST_EXPIRED"
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    [issue] = issues(hass, entry, "clock_skew")
    assert issue.translation_placeholders == {"name": "Codie"}

    fake_codie.auth_error = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert issues(hass, entry, "clock_skew") == []


async def test_bad_signature_with_the_codie_clock_far_off_raises_the_clock_issue(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.auth_error = "INVALID_SIGNATURE"
    fake_codie.date_skew = 3600
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert len(issues(hass, entry, "clock_skew")) == 1


async def test_clock_skew_while_running_is_cleared_when_the_executor_reconnects(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)
    assert issues(hass, entry, "clock_skew") == []

    fake_codie.auth_error = "REQUEST_EXPIRED"
    await fake_codie.drop_executor()
    await wait_for(lambda: len(issues(hass, entry, "clock_skew")) == 1)

    # The executor's handshake is signed, so getting back in shows the clocks agree again.
    fake_codie.auth_error = None
    await wait_connected(hass, fake_codie, True)
    assert issues(hass, entry, "clock_skew") == []


async def test_rejected_credentials_are_not_a_clock_issue(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.auth_error = "LOCAL_SECRET_MISMATCH"
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert issues(hass, entry, "clock_skew") == []


# --- Tools left out ----------------------------------------------------------------------


class VacuumStart(intent.IntentHandler):
    intent_type = "HassVacuumStart"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        return intent_obj.create_response()


async def test_a_missing_assist_tool_raises_an_issue_naming_the_tool(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    intent.async_remove(hass, "HassVacuumStart")

    entry = await setup(hass, fake_codie)

    [issue] = issues(hass, entry, "tool_missing")
    assert issue.translation_placeholders == {
        "name": "Codie",
        "tool": "vacuum_start",
        "assist_tool": "HassVacuumStart",
    }

    # Home Assistant offers it again, so the Tool is back in the catalog.
    intent.async_register(hass, VacuumStart())
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "vacuum"})
    await wait_for(lambda: issues(hass, entry, "tool_missing") == [])


async def test_tools_whose_integration_is_not_loaded_raise_no_issue(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    # Without a vacuum, light or media player, their Tools are rightly missing. Light's
    # intents also wait for the `intent` integration, which registers them.
    assert await async_setup_component(hass, "light", {})
    entry = await setup(hass, fake_codie)

    assert issues(hass, entry, "tool_missing") == []
    assert issues(hass, entry, "tool_schema_limit") == []


class OversizedLiveContext(FakeLiveContext):
    """A live context tool with more parameters than the Codie accepts."""

    parameters = probatio.Schema({probatio.Optional(f"p{i}"): str for i in range(70)})


async def test_parameters_over_the_codie_limits_raise_an_issue_saying_why(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    api_tools: list[Any] = [OversizedLiveContext()]
    with patch(
        "custom_components.codie.tools._assist_api_tools",
        side_effect=lambda *_: (api_tools, llm.selector_serializer),
    ):
        entry = await setup(hass, fake_codie)
        [issue] = issues(hass, entry, "tool_schema_limit")
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders["tool"] == "get_home_state"
        assert "64" in issue.translation_placeholders["reason"]

        api_tools[:] = [FakeLiveContext()]
        hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "llm"})
        await wait_for(lambda: issues(hass, entry, "tool_schema_limit") == [])


# --- catalog rejected --------------------------------------------------------------------


async def test_a_rejected_catalog_raises_an_issue_cleared_once_it_is_accepted(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.put_error = (400, "INVALID_TOOL_SCHEMA")
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    [issue] = issues(hass, entry, "catalog_rejected")
    assert issue.translation_placeholders is not None
    assert "INVALID_TOOL_SCHEMA" in issue.translation_placeholders["error"]

    fake_codie.put_error = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert issues(hass, entry, "catalog_rejected") == []


async def test_a_stale_revision_is_not_a_rejected_catalog(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.put_conflicts = 10
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert issues(hass, entry, "catalog_rejected") == []


async def test_removing_the_entry_removes_its_issues(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.put_error = (400, "INVALID_TOOL_SCHEMA")
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert len(issues(hass, entry, "catalog_rejected")) == 1

    await hass.config_entries.async_remove(entry.entry_id)

    assert issues(hass, entry, "catalog_rejected") == []


class TurnOn(intent.IntentHandler):
    intent_type = "HassTurnOn"

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        return intent_obj.create_response()


async def refuse_a_new_catalog(hass: HomeAssistant, fake: FakeCodie) -> MockConfigEntry:
    """Set up, then add an Assist tool so the catalog changes, and have the Codie refuse it."""
    entry = await setup(hass, fake)
    await wait_connected(hass, fake, True)
    fake.put_error = (400, "INVALID_TOOL_SCHEMA")
    intent.async_register(hass, TurnOn())
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "light"})
    await wait_for(lambda: len(issues(hass, entry, "catalog_rejected")) == 1)
    return entry


@CLOSES_A_LIVE_SOCKET
async def test_a_catalog_refused_while_running_is_retried_until_accepted(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await refuse_a_new_catalog(hass, fake_codie)

    # Nothing else loads; the retry alone gets the catalog through.
    fake_codie.put_error = None
    await wait_for(lambda: issues(hass, entry, "catalog_rejected") == [])
    tools = fake_codie.tool_integrations[INTEGRATION_ID]["catalog"]["tools"]
    assert "turn_on" in {tool["name"] for tool in tools}


@CLOSES_A_LIVE_SOCKET
async def test_without_the_retry_a_refused_catalog_stays_refused_and_unload_cancels_it(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    with patch("custom_components.codie.REFRESH_RETRY_S", 3600):
        entry = await refuse_a_new_catalog(hass, fake_codie)
        fake_codie.put_error = None
        await asyncio.sleep(0.5)
        assert len(issues(hass, entry, "catalog_rejected")) == 1

        # The pending retry goes with the entry; a lingering timer would fail the test.
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@CLOSES_A_LIVE_SOCKET
async def test_going_back_to_the_accepted_tools_clears_the_refused_catalog(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    with patch("custom_components.codie.REFRESH_RETRY_S", 3600):
        entry = await refuse_a_new_catalog(hass, fake_codie)

        # The Codie already holds the Tools it accepted, so nothing is refused any more.
        intent.async_remove(hass, "HassTurnOn")
        hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "light"})
        await wait_for(lambda: issues(hass, entry, "catalog_rejected") == [])
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_tool_issues_restored_from_storage_are_cleared(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    intent.async_remove(hass, "HassVacuumStart")
    entry = await setup(hass, fake_codie)
    [issue] = issues(hass, entry, "tool_missing")
    # After a restart an issue comes back from storage inactive and without its text.
    registry = ir.async_get(hass)
    key = (DOMAIN, issue.issue_id)
    registry.issues[key] = dataclasses.replace(issue, active=False, translation_key=None)

    intent.async_register(hass, VacuumStart())
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "vacuum"})
    await wait_for(lambda: key not in registry.issues)


async def test_a_disabled_entry_hides_its_issues(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.put_error = (400, "INVALID_TOOL_SCHEMA")
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    [issue] = issues(hass, entry, "catalog_rejected")

    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)

    assert ir.async_get(hass).async_is_hidden(issue)


async def test_a_server_error_on_the_catalog_is_not_a_rejected_catalog(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.put_error = (503, "SERVICE_UNAVAILABLE")
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert issues(hass, entry, "catalog_rejected") == []
