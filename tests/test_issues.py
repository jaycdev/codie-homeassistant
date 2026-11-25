"""Repair issues for clock skew, a refused catalog and Tools left out, against the fake Codie."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import EVENT_COMPONENT_LOADED, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.codie.api import CodieApiError
from custom_components.codie.const import DOMAIN
from custom_components.codie.schema import SchemaLimitError

from .fake_codie import FakeCodie
from .test_resilience import CLOSES_A_LIVE_SOCKET
from .test_tools import INTEGRATION_ID, make_entry, setup, wait_connected

PUT = "custom_components.codie.api.CodieClient.async_put_tool_integration"


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
    """Assist reads the Expose list from `homeassistant` and its API comes from `llm`."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})


def issue(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_{name}")


def entry_issues(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    prefix = f"{entry.entry_id}_"
    return {
        issue_id.removeprefix(prefix)
        for domain, issue_id in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith(prefix)
    }


def seed_issue(hass: HomeAssistant, entry_id: str) -> None:
    """An issue for `entry_id`, as the integration would have raised it."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{entry_id}_clock_skew",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="clock_skew",
    )


def catalog_tools(fake: FakeCodie) -> dict[str, dict[str, Any]]:
    tools = fake.tool_integrations[INTEGRATION_ID]["catalog"]["tools"]
    return {tool["name"]: tool for tool in tools}


async def wait_for(condition: Callable[[], bool]) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


def register_intent(hass: HomeAssistant, intent_type: str) -> None:
    class Handler(intent.IntentHandler):
        async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
            return intent_obj.create_response()

    handler = Handler()
    handler.intent_type = intent_type
    intent.async_register(hass, handler)


# --- clock skew ----------------------------------------------------------------------------


async def test_clock_skew_at_setup_raises_an_issue_until_a_request_works(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.auth_error = "REQUEST_EXPIRED"
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    found = issue(hass, entry, "clock_skew")
    assert found is not None
    assert found.translation_key == "clock_skew"
    assert found.is_fixable is False
    assert found.translation_placeholders == {"name": "Codie"}

    fake_codie.auth_error = None
    # A reload that returns True has loaded the entry.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert issue(hass, entry, "clock_skew") is None


async def test_bad_signature_with_the_codie_clock_far_off_raises_the_issue(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.auth_error = "INVALID_SIGNATURE"
    fake_codie.date_skew = 3600
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert issue(hass, entry, "clock_skew") is not None


async def test_clock_skew_while_running_clears_once_the_executor_connects(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)
    assert issue(hass, entry, "clock_skew") is None

    fake_codie.auth_error = "REQUEST_EXPIRED"
    await fake_codie.drop_executor()
    await wait_for(lambda: issue(hass, entry, "clock_skew") is not None)

    fake_codie.auth_error = None
    await wait_connected(hass, fake_codie, True)
    assert issue(hass, entry, "clock_skew") is None


# --- refused catalog -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("refusal", "error"),
    [
        (
            CodieApiError(400, "INVALID_TOOL_SCHEMA", "tools[3].parameters is too deep"),
            "400 INVALID_TOOL_SCHEMA: tools[3].parameters is too deep",
        ),
        (CodieApiError(422, None, "too many tools"), "422: too many tools"),
        (CodieApiError(400, "BAD_CATALOG", None), "400 BAD_CATALOG"),
        (CodieApiError(413, None, None), "413"),
    ],
)
async def test_refused_catalog_raises_an_issue_until_the_codie_accepts_it(
    hass: HomeAssistant, fake_codie: FakeCodie, refusal: CodieApiError, error: str
) -> None:
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    with patch(PUT, side_effect=refusal):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    found = issue(hass, entry, "catalog_rejected")
    assert found is not None
    assert found.translation_key == "catalog_rejected"
    assert found.translation_placeholders == {"name": "Codie", "error": error}

    # A reload that returns True has loaded the entry.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert issue(hass, entry, "catalog_rejected") is None


@pytest.mark.parametrize("status", [401, 408, 429, 500, 503])
async def test_a_put_that_fails_for_another_reason_is_not_a_refused_catalog(
    hass: HomeAssistant, fake_codie: FakeCodie, status: int
) -> None:
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    with patch(PUT, side_effect=CodieApiError(status, "SOME_CODE", "no")):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert issue(hass, entry, "catalog_rejected") is None


async def test_stale_revision_is_not_a_refused_catalog(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.tool_integrations[INTEGRATION_ID] = {"revision": 1, "catalog": {"tools": []}}
    fake_codie.put_conflicts = 5
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert issue(hass, entry, "catalog_rejected") is None


async def test_a_failed_read_of_the_catalog_is_not_a_refused_catalog(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.codie.api.CodieClient.async_get_tool_integration",
        side_effect=CodieApiError(400, "BAD_REQUEST", "no"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert issue(hass, entry, "catalog_rejected") is None


# --- Tools left out ------------------------------------------------------------------------


async def test_device_intents_that_are_missing_raise_nothing(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    """Without a vacuum, fan or media player integration their Tools are left out by design."""
    register_intent(hass, "HassTurnOn")
    register_intent(hass, "HassTurnOff")
    register_intent(hass, "HassSetPosition")

    entry = await setup(hass, fake_codie)

    tools = catalog_tools(fake_codie)
    assert "vacuum_start" not in tools
    assert "media_control" not in tools
    # Not offered for the Codie's AI language, which is no reason for a repair either.
    assert "timer_start" not in tools
    assert entry_issues(hass, entry) == set()


async def test_media_control_with_only_some_intents_is_offered_without_an_issue(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    register_intent(hass, "HassMediaPause")
    register_intent(hass, "HassMediaNext")

    entry = await setup(hass, fake_codie)

    media = catalog_tools(fake_codie)["media_control"]
    assert media["parameters"]["properties"]["action"]["enum"] == ["pause", "next"]
    assert issue(hass, entry, "tool_left_out_media_control") is None


@CLOSES_A_LIVE_SOCKET
async def test_missing_core_intent_raises_an_issue_until_the_tool_is_offered(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    found = issue(hass, entry, "tool_left_out_turn_on")
    assert found is not None
    assert found.translation_key == "core_tool_missing"
    assert found.translation_placeholders == {
        "name": "Codie",
        "tool": "turn_on",
        "assist_tool": "HassTurnOn",
        "error": "Assist tool HassTurnOn is not available",
    }
    assert entry_issues(hass, entry) == {
        "tool_left_out_turn_on",
        "tool_left_out_turn_off",
        "tool_left_out_set_position",
    }

    # Home Assistant's own intents come with the `intent` integration.
    assert await async_setup_component(hass, "intent", {})
    await wait_for(lambda: not entry_issues(hass, entry))


@CLOSES_A_LIVE_SOCKET
async def test_tool_that_does_not_fit_raises_an_issue_with_the_reason(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    with patch(
        "custom_components.codie.tools.from_probatio",
        side_effect=SchemaLimitError("nested 6 levels deep, the Codie allows 5"),
    ):
        entry = await setup(hass, fake_codie)

    found = issue(hass, entry, "tool_left_out_get_home_state")
    assert found is not None
    assert found.translation_key == "tool_unsupported"
    assert found.translation_placeholders == {
        "name": "Codie",
        "tool": "get_home_state",
        "assist_tool": "GetLiveContext",
        "error": "nested 6 levels deep, the Codie allows 5",
    }

    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "light"})
    await wait_for(lambda: issue(hass, entry, "tool_left_out_get_home_state") is None)


async def test_tools_left_out_wait_until_home_assistant_has_started(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    """While Home Assistant starts, integrations are still adding their Assist tools."""
    hass.set_state(CoreState.starting)
    entry = await setup(hass, fake_codie)

    assert not entry_issues(hass, entry)

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await wait_for(lambda: issue(hass, entry, "tool_left_out_turn_on") is not None)


# --- the entry's lifecycle -----------------------------------------------------------------


async def test_unloading_the_entry_clears_its_issues(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    entry = await setup(hass, fake_codie)
    seed_issue(hass, entry.entry_id)
    seed_issue(hass, "other")
    assert "clock_skew" in entry_issues(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not entry_issues(hass, entry)
    # Another Codie's issues stay.
    assert ir.async_get(hass).async_get_issue(DOMAIN, "other_clock_skew") is not None


async def test_disabling_an_entry_that_is_retrying_setup_clears_its_issues(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.auth_error = "REQUEST_EXPIRED"
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry_issues(hass, entry) == {"clock_skew"}

    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()

    assert not entry_issues(hass, entry)


async def test_removing_the_entry_clears_its_issues(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    fake_codie.auth_error = "REQUEST_EXPIRED"
    entry = make_entry(fake_codie)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry_issues(hass, entry) == {"clock_skew"}

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not entry_issues(hass, entry)
