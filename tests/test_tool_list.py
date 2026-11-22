"""The full fixed Tool list: catalog contents, schema limits, and calls through Assist."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent, llm, selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_mock_service

from custom_components.codie.schema import check_limits

from .fake_codie import FakeCodie
from .test_tools import INTEGRATION_ID, setup, wait_connected

ALL_TOOLS = {
    "get_home_state",
    "turn_on",
    "turn_off",
    "light_set",
    "climate_set_temp",
    "set_position",
    "fan_set_speed",
    "media_pause",
    "media_unpause",
    "media_next",
    "media_previous",
    "set_volume",
    "media_search_play",
    "vacuum_start",
    "list_add_item",
    "todo_get_items",
}
READ_TOOLS = {"get_home_state", "todo_get_items"}


async def setup_assist(hass: HomeAssistant) -> None:
    """Load the integrations whose Assist intents the Tools wrap."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "intent", {})
    for domain in ("light", "climate", "cover", "fan", "media_player", "vacuum", "todo"):
        assert await async_setup_component(hass, domain, {})
    # Conversation registers the other integrations' intents, so it comes last.
    assert await async_setup_component(hass, "conversation", {})


def add_entity(
    hass: HomeAssistant,
    domain: str,
    name: str,
    exposed: bool,
    unique_id: str | None = None,
    **attributes: Any,
) -> str:
    unique_id = unique_id or name
    # Assist matches the registry name, so the entry needs one, like a real entity has.
    entry = er.async_get(hass).async_get_or_create(
        domain, "test", unique_id, suggested_object_id=unique_id, original_name=name
    )
    hass.states.async_set(entry.entity_id, "off", {"friendly_name": name, **attributes})
    async_expose_entity(hass, "conversation", entry.entity_id, exposed)
    return entry.entity_id


def registered_tools(fake: FakeCodie) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = fake.tool_integrations[INTEGRATION_ID]["catalog"]["tools"]
    return tools


async def test_registers_the_full_fixed_list(hass: HomeAssistant, fake_codie: FakeCodie) -> None:
    await setup_assist(hass)

    await setup(hass, fake_codie)

    tools = registered_tools(fake_codie)
    assert {t["name"] for t in tools} == ALL_TOOLS
    assert len(tools) <= 16
    for tool in tools:
        assert tool["risk"] == ("read" if tool["name"] in READ_TOOLS else "reversible")
        assert tool["confirmation_required"] is False
        assert tool["timeout_ms"] == 10000


async def test_every_schema_is_within_the_codie_limits(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    await setup(hass, fake_codie)

    for tool in registered_tools(fake_codie):
        for key in ("parameters", "result_schema"):
            schema = tool[key]
            check_limits(schema)
            assert schema["type"] == "object", tool["name"]
            text = json.dumps(schema)
            for banned in ("$ref", "anyOf", "oneOf", "allOf", "pattern"):
                assert banned not in text, (tool["name"], banned)
        assert len(tool["description"]) <= 300


async def test_climate_schema_keeps_the_required_temperature(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    await setup(hass, fake_codie)

    tool = next(t for t in registered_tools(fake_codie) if t["name"] == "climate_set_temp")

    assert tool["parameters"]["required"] == ["temperature"]
    assert tool["parameters"]["properties"]["temperature"]["type"] == "number"


async def test_turn_on_and_off_act_on_an_exposed_light(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    light = add_entity(hass, "light", "bedroom_tv_lamp", True)
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    on = await fake_codie.invoke(INTEGRATION_ID, "turn_on", {"name": "bedroom_tv_lamp"})
    off = await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "bedroom_tv_lamp"})

    assert on["status"] == "succeeded"
    assert on["data"]["success"] is True
    assert off["status"] == "succeeded"
    assert [c.data["entity_id"] for c in on_calls] == [[light]]
    assert [c.data["entity_id"] for c in off_calls] == [[light]]


async def test_climate_set_temperature_acts_on_an_exposed_entity(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    ac = add_entity(hass, "climate", "bedroom_ac", True, supported_features=1)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(
        INTEGRATION_ID, "climate_set_temp", {"name": "bedroom_ac", "temperature": 24}
    )

    assert result["status"] == "succeeded"
    assert [(c.data["entity_id"], c.data["temperature"]) for c in calls] == [(ac, 24)]


async def test_a_call_on_a_hidden_entity_fails_and_changes_nothing(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen_lamp", True)
    add_entity(hass, "light", "secret_lamp", False)
    calls = async_mock_service(hass, "light", "turn_on")
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(INTEGRATION_ID, "turn_on", {"name": "secret_lamp"})

    assert result["status"] == "failed"
    assert result["code"]
    assert result["data"]["success"] is False
    assert calls == []


async def test_ambiguous_names_ask_for_clarification(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    for unique_id in ("lamp_one", "lamp_two"):
        add_entity(hass, "light", "lamp", True, unique_id=unique_id)
    calls = async_mock_service(hass, "light", "turn_on")
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(INTEGRATION_ID, "turn_on", {"name": "lamp"})

    assert result["status"] == "needs_clarification"
    assert result["data"]["success"] is False
    assert calls == []


async def test_bad_arguments_fail_without_touching_home_assistant(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    add_entity(hass, "climate", "bedroom_ac", True, supported_features=1)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(
        INTEGRATION_ID, "climate_set_temp", {"name": "bedroom_ac", "temperature": "hot"}
    )

    assert result["status"] == "failed"
    assert result["code"] == "invalid_arguments"
    assert calls == []


async def test_a_missing_assist_tool_leaves_its_codie_tool_out(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    intent.async_remove(hass, "HassVacuumStart")

    await setup(hass, fake_codie)

    assert {t["name"] for t in registered_tools(fake_codie)} == ALL_TOOLS - {"vacuum_start"}


async def test_other_assist_tools_are_never_offered_or_callable(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen_lamp", True)
    hass.states.async_set("script.movie_time", "off", {"friendly_name": "Movie time"})
    async_expose_entity(hass, "conversation", "script.movie_time", True)
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(INTEGRATION_ID, "HassToggle", {"name": "kitchen_lamp"})

    assert result["code"] == "unknown_tool"
    assert {t["name"] for t in registered_tools(fake_codie)} <= ALL_TOOLS


def test_intent_responses_are_reduced_to_the_stable_shape() -> None:
    from custom_components.codie.tools import _shape_result

    data = _shape_result(
        {
            "response_type": "action_done",
            "data": {"success": [{"name": "Kitchen lamp", "type": "entity", "id": "light.k"}]},
            "speech": {},
        }
    )

    assert data == {"success": True, "result": "Done: Kitchen lamp"}


def test_assist_failures_become_tool_errors() -> None:
    import pytest

    from custom_components.codie.errors import ToolError
    from custom_components.codie.tools import _shape_result

    with pytest.raises(ToolError) as plain:
        _shape_result({"success": False, "error": "To-do list not found"})
    with pytest.raises(ToolError) as intent_error:
        _shape_result(
            {
                "response_type": "error",
                "data": {"code": "no_valid_targets"},
                "speech": {"plain": {"speech": "Nothing matched"}},
            }
        )

    assert plain.value.code == "assist_failed"
    assert str(plain.value) == "To-do list not found"
    assert intent_error.value.code == "no_valid_targets"
    assert str(intent_error.value) == "Nothing matched"


async def test_failed_calls_are_logged_with_their_cause(
    hass: HomeAssistant, fake_codie: FakeCodie, caplog: pytest.LogCaptureFixture
) -> None:
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen_lamp", True)
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    await fake_codie.invoke(INTEGRATION_ID, "turn_on", {"name": "nonexistent"})

    [record] = [r for r in caplog.records if "Tool turn_on" in r.getMessage()]
    assert record.levelname == "WARNING"
    assert "target_not_found" in record.getMessage()
    assert "nonexistent" in record.getMessage()


async def test_a_domain_with_nothing_exposed_reports_the_target_not_the_tool(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    add_entity(hass, "light", "kitchen_lamp", True)
    add_entity(hass, "climate", "study_ac", False, supported_features=1)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(
        INTEGRATION_ID, "climate_set_temp", {"name": "study_ac", "temperature": 24}
    )

    assert result["status"] == "failed"
    assert result["code"] == "target_not_found"
    assert calls == []


@dataclass
class FakeToolResult:
    """What Home Assistant 2026.11 tools return instead of a plain dict."""

    data: dict[str, Any]
    error: bool = False


class FakeLiveContext(llm.Tool):
    """Stands in for 2026.11's prefixed `homeassistant__GetLiveContext`."""

    name = "homeassistant__GetLiveContext"
    description = "Provides real-time information about the CURRENT state of devices."
    parameters = probatio.Schema(
        {
            probatio.Optional("name"): str,
            probatio.Optional("domain"): probatio.Any(str, [str]),
            probatio.Optional("area"): str,
        }
    )
    reply = FakeToolResult({"result": "Live Context: study AC is on"})

    async def async_call(self, hass: Any, tool_input: Any, llm_context: Any) -> Any:
        self.seen = tool_input
        return self.reply


async def test_prefixed_tools_and_tool_result_objects_from_2026_11(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    live = FakeLiveContext()
    with patch(
        "custom_components.codie.tools._assist_api_tools",
        return_value=([live], llm.selector_serializer),
    ):
        await setup(hass, fake_codie)
        await wait_connected(hass, fake_codie, True)
        result = await fake_codie.invoke(INTEGRATION_ID, "get_home_state", {"area": "study"})

    tool = next(t for t in registered_tools(fake_codie) if t["name"] == "get_home_state")
    assert tool["parameters"]["properties"]["domain"] == {"type": "string"}
    assert result["status"] == "succeeded"
    assert result["data"] == {"success": True, "result": "Live Context: study AC is on"}
    assert live.seen.tool_name == "homeassistant__GetLiveContext"
    assert live.seen.tool_args == {"area": "study"}


class SelectorLiveContext(FakeLiveContext):
    """A tool whose parameters are Home Assistant selectors, which probatio cannot render."""

    parameters = probatio.Schema(
        {
            probatio.Optional("rgb"): selector.ColorRGBSelector(),
            probatio.Optional("level"): selector.NumberSelector({"min": 0, "max": 10}),
        }
    )


def codie_serializer(schema: Any) -> Any:
    """Renders an RGB selector its own way and leaves the rest to Home Assistant's."""
    if isinstance(schema, selector.ColorRGBSelector):
        return {"type": "string", "description": "hex colour"}
    return llm.selector_serializer(schema)


STANDARD_RGB = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
CUSTOM_RGB = {"type": "string", "description": "hex colour"}


@pytest.mark.parametrize(
    ("api", "rgb"),
    [
        ("with_serializer", CUSTOM_RGB),
        ("without_serializer", STANDARD_RGB),
        ("unreadable", STANDARD_RGB),
    ],
)
async def test_selector_parameters_are_rendered_whether_or_not_the_assist_api_is_readable(
    hass: HomeAssistant, fake_codie: FakeCodie, api: str, rgb: dict[str, Any]
) -> None:
    await setup_assist(hass)
    tool = SelectorLiveContext()
    serializer = codie_serializer if api == "with_serializer" else None
    instance = SimpleNamespace(tools=[tool], custom_serializer=serializer)
    get_api = (
        patch("custom_components.codie.tools.llm.async_get_api", side_effect=HomeAssistantError)
        if api == "unreadable"
        else patch("custom_components.codie.tools.llm.async_get_api", return_value=instance)
    )
    with (
        get_api,
        patch("custom_components.codie.tools._fallback_tool", return_value=tool),
    ):
        await setup(hass, fake_codie)

    parameters = next(t for t in registered_tools(fake_codie) if t["name"] == "get_home_state")[
        "parameters"
    ]
    assert parameters["properties"] == {
        "rgb": rgb,
        "level": {"type": "number", "minimum": 0, "maximum": 10},
    }


async def test_the_fallback_todo_tool_offers_its_list_name_as_a_string(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    with patch("custom_components.codie.tools.llm.async_get_api", side_effect=HomeAssistantError):
        await setup(hass, fake_codie)

    parameters = next(t for t in registered_tools(fake_codie) if t["name"] == "todo_get_items")[
        "parameters"
    ]
    assert parameters["properties"]["todo_list"] == {"type": "string"}
    assert "todo_list" in parameters["required"]


async def test_a_typed_todo_list_is_not_overridden(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    from homeassistant.components.todo.llm import TodoGetItemsTool

    await setup_assist(hass)
    instance = SimpleNamespace(tools=[TodoGetItemsTool(["Shopping"])], custom_serializer=None)
    with patch("custom_components.codie.tools.llm.async_get_api", return_value=instance):
        await setup(hass, fake_codie)

    parameters = next(t for t in registered_tools(fake_codie) if t["name"] == "todo_get_items")[
        "parameters"
    ]
    assert parameters["properties"]["todo_list"] == {"type": "string", "enum": ["Shopping"]}
    assert "todo_list" in parameters["required"]


async def test_tool_result_errors_become_failed_calls(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    live = FakeLiveContext()
    live.reply = FakeToolResult({"error": "No exposed entities matched name 'x'"}, error=True)
    with patch(
        "custom_components.codie.tools._assist_api_tools",
        return_value=([live], llm.selector_serializer),
    ):
        await setup(hass, fake_codie)
        await wait_connected(hass, fake_codie, True)
        result = await fake_codie.invoke(INTEGRATION_ID, "get_home_state", {"name": "x"})

    assert result["status"] == "failed"
    assert result["code"] == "assist_failed"
    assert "No exposed entities" in result["data"]["error"]


def test_tool_result_items_are_returned_as_json_text() -> None:
    from custom_components.codie.tools import _shape_result

    data = _shape_result(FakeToolResult({"items": [{"summary": "milk"}]}))

    assert data == {"success": True, "result": '[{"summary": "milk"}]'}


def test_prefixed_names_are_found_by_either_spelling() -> None:
    from custom_components.codie.tools import find_api_tool

    class Todo(llm.Tool):
        name = "todo__get_items"

        async def async_call(self, hass: Any, tool_input: Any, llm_context: Any) -> Any:
            return {}

    todo = Todo()

    assert find_api_tool([todo], "todo_get_items") is todo
    assert find_api_tool([], "todo_get_items") is None


async def test_device_class_guesses_are_not_offered_and_are_ignored(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    light = add_entity(hass, "light", "study_lamp", True)
    calls = async_mock_service(hass, "light", "turn_off")
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    for name in ("turn_on", "turn_off"):
        tool = next(t for t in registered_tools(fake_codie) if t["name"] == name)
        assert "device_class" not in tool["parameters"]["properties"]

    result = await fake_codie.invoke(
        INTEGRATION_ID,
        "turn_off",
        {"name": "study_lamp", "device_class": ["switch", "outlet"]},
    )

    assert result["status"] == "succeeded"
    assert [c.data["entity_id"] for c in calls] == [[light]]


async def test_a_missed_name_lists_the_exposed_names_to_try(
    hass: HomeAssistant, fake_codie: FakeCodie
) -> None:
    await setup_assist(hass)
    add_entity(hass, "light", "study_lamp", True)
    add_entity(hass, "climate", "study_ac", True, supported_features=1)
    add_entity(hass, "light", "secret_lamp", False)
    await setup(hass, fake_codie)
    await wait_connected(hass, fake_codie, True)

    result = await fake_codie.invoke(INTEGRATION_ID, "turn_off", {"name": "aid"})

    assert result["code"] == "target_not_found"
    error = result["data"]["error"]
    assert "study_lamp" in error
    assert "study_ac" in error
    assert "secret_lamp" not in error
