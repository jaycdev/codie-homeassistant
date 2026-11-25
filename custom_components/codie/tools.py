"""The Tools the Codie's Character can call, each backed by an Assist LLM API tool."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import probatio
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent, llm
from homeassistant.helpers.importlib import async_import_module

from .api import CodieApiError, CodieClient
from .errors import (
    ERROR_AMBIGUOUS_TARGET,
    ERROR_ASSIST_FAILED,
    ERROR_HOME_ASSISTANT,
    ERROR_INTENT_FAILED,
    ERROR_INVALID_ARGUMENTS,
    ERROR_TARGET_NOT_FOUND,
    ERROR_TOOL_UNAVAILABLE,
    ERROR_UNKNOWN_TOOL,
    ToolError,
    ToolNeedsClarification,
)
from .schema import SchemaLimitError, from_probatio
from .timers import (
    MAX_COMMAND_CHARS,
    MAX_NAME_CHARS,
    TIMERS_UNAVAILABLE,
    CodieTimers,
    timers_supported,
)

_LOGGER = logging.getLogger(__name__)

TOOL_VERSION = "v1"
DEFAULT_TIMEOUT_MS = 10000
# The Codie accepts at most 16 KiB of result data.
MAX_RESULT_BYTES = 12000
MAX_ERROR_CHARS = 1000
MAX_DESCRIPTION_CHARS = 200
MAX_NAMES_CHARS = 600
REGISTER_ATTEMPTS = 3

ASSIST_LIVE_CONTEXT = "GetLiveContext"
ASSIST_TODO_GET_ITEMS = "todo_get_items"

# Every Tool returns this one small shape. Assist's own results are reduced to it.
_ASSIST_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "result": {"type": "string", "maxLength": MAX_RESULT_BYTES},
        "error": {"type": "string", "maxLength": MAX_ERROR_CHARS},
    },
    "required": ["success"],
    "additionalProperties": False,
}


# In place of an Assist tool name: the integration handles the Tool itself.
INTEGRATION = ""


@dataclass(frozen=True)
class CodieTool:
    """One Tool offered to the Character and the Assist tool that runs it."""

    name: str
    assist_name: str
    risk: str
    # Replaces the Assist description when set.
    description: str | None = None
    # Parameters the Character is not offered and whose values are dropped. A guessed filter
    # such as device_class silently excludes the very entity the user named.
    hidden: tuple[str, ...] = ()
    # Replaces the generic message when Assist does not offer this Tool.
    unavailable: str | None = None
    # Makes the Tool take a required `action` and run the Assist intent that action maps to
    # (ADR-0003). All of them take the same parameters; those of the first one offered are used.
    actions: Mapping[str, str] = field(default_factory=dict)
    # The start of the description when exposed scripts are listed after it. Assist's own
    # description is too long to leave room for their names.
    scripts_intro: str | None = None
    # The parameters of a Tool the integration handles itself, such as the Timer Tools. It has
    # no Assist tool to take them from, so `assist_name` is `INTEGRATION`.
    parameters: Mapping[str, Any] | None = None
    # Actions of a combined Tool that only read, so the Tool is not a read Tool as a whole.
    read_actions: tuple[str, ...] = ()

    def assist_name_for(self, arguments: Mapping[str, Any]) -> str | None:
        """The Assist tool a call with `arguments` runs, or None if there is none.

        That is the intent the `action` maps to for a combined Tool, and none for a Tool the
        integration handles itself or for an `action` that is missing or unknown.
        """
        if not self.actions:
            return self.assist_name or None
        action = arguments.get("action")
        return self.actions.get(action) if isinstance(action, str) else None

    def reads(self, arguments: Mapping[str, Any]) -> bool:
        """Whether a call with `arguments` only reads."""
        return self.risk == "read" or arguments.get("action") in self.read_actions


TOOLS: tuple[CodieTool, ...] = (
    CodieTool(
        "get_home_state",
        ASSIST_LIVE_CONTEXT,
        "read",
        description=(
            "Get the current state of the user's exposed smart home devices, sensors and areas. "
            "Call it to answer questions about the home, and whenever you are unsure of a "
            "device name or its state."
        ),
    ),
    CodieTool(
        "turn_on",
        "HassTurnOn",
        "reversible",
        hidden=("device_class",),
        # get_home_state leaves scripts out, so the Character finds them in this description.
        scripts_intro="Turns on/opens/locks a device or entity, or runs a script. Scripts: ",
    ),
    CodieTool("turn_off", "HassTurnOff", "reversible", hidden=("device_class",)),
    CodieTool("light_set", "HassLightSet", "reversible"),
    CodieTool("climate_set_temp", "HassClimateSetTemperature", "reversible"),
    CodieTool("set_position", "HassSetPosition", "reversible"),
    CodieTool("fan_set_speed", "HassFanSetSpeed", "reversible"),
    CodieTool(
        "media_control",
        "HassMediaPause",
        "reversible",
        description=(
            "Pauses, resumes, or skips to the next or previous item of a media player. "
            "Set action to pause, resume, next or previous."
        ),
        actions={
            "pause": "HassMediaPause",
            "resume": "HassMediaUnpause",
            "next": "HassMediaNext",
            "previous": "HassMediaPrevious",
        },
    ),
    CodieTool("set_volume", "HassSetVolume", "reversible"),
    CodieTool("media_search_play", "HassMediaSearchAndPlay", "reversible"),
    CodieTool("vacuum_start", "HassVacuumStart", "reversible"),
    CodieTool("list_add_item", "HassListAddItem", "reversible"),
    CodieTool(
        "todo_get_items",
        ASSIST_TODO_GET_ITEMS,
        "read",
        unavailable="No to-do list is exposed to Assist",
    ),
    CodieTool(
        "timer_start",
        INTEGRATION,
        "reversible",
        description=(
            "Runs a home-control Command after a delay, such as 'turn off the kitchen lamp' in "
            "20 minutes. At most 24 hours. Give it a name to refer to it later."
        ),
        unavailable=TIMERS_UNAVAILABLE,
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "What to do when the Timer ends, as a short sentence",
                    "minLength": 1,
                    "maxLength": MAX_COMMAND_CHARS,
                },
                "hours": {"type": "integer", "minimum": 0, "maximum": 24},
                "minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
                "seconds": {"type": "integer", "minimum": 0, "maximum": 86400},
                "name": {"type": "string", "maxLength": MAX_NAME_CHARS},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    ),
    CodieTool(
        "timer_control",
        INTEGRATION,
        "reversible",
        description=(
            "Cancels a running Timer, or tells how much time is left on one or on all of them. "
            "Set action to cancel or status."
        ),
        unavailable=TIMERS_UNAVAILABLE,
        read_actions=("status",),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["cancel", "status"]},
                "name": {"type": "string", "maxLength": MAX_NAME_CHARS},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
)


# Where the tools that are not plain intents live. Home Assistant 2026.11 moved them out of
# `helpers/llm.py` into their integrations and prefixed their names with the integration.
_API_NAMES = {
    ASSIST_LIVE_CONTEXT: ("GetLiveContext", "homeassistant__GetLiveContext"),
    ASSIST_TODO_GET_ITEMS: ("todo_get_items", "todo__get_items"),
}
_FALLBACK_CLASSES: dict[str, tuple[tuple[str, str, tuple[Any, ...]], ...]] = {
    ASSIST_LIVE_CONTEXT: (
        ("homeassistant.components.homeassistant.llm", "GetLiveContextTool", ()),
        ("homeassistant.helpers.llm", "GetLiveContextTool", ()),
    ),
    ASSIST_TODO_GET_ITEMS: (
        ("homeassistant.components.todo.llm", "TodoGetItemsTool", ([],)),
        ("homeassistant.helpers.llm", "TodoGetItemsTool", ([],)),
    ),
}


def _llm_context(language: str | None = None) -> llm.LLMContext:
    """The context for every call: the Assist API only ever sees what Assist exposes.

    `language` is the Codie's AI language, the one the Character speaks and understands.
    """
    return llm.LLMContext(
        platform="codie",
        context=None,
        language=language,
        assistant="conversation",
        device_id=None,
    )


async def _assist_api_tools(
    hass: HomeAssistant, language: str | None = None
) -> tuple[list[llm.Tool], Callable[[Any], Any]]:
    """The Assist API's tools and the serializer for their schemas.

    The serializer is Home Assistant's selector one unless the API names its own, so a Tool's
    schema does not depend on whether the Assist API could be read.
    """
    try:
        api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, _llm_context(language))
    except HomeAssistantError as err:
        _LOGGER.debug("Cannot read the Assist API: %s", err)
        return [], llm.selector_serializer
    return list(api.tools), api.custom_serializer or llm.selector_serializer


def find_api_tool(tools: list[llm.Tool], assist_name: str) -> llm.Tool | None:
    """The Assist API's tool for `assist_name`, under its old or its prefixed name."""
    names = _API_NAMES.get(assist_name, (assist_name,))
    return next((tool for tool in tools if tool.name in names), None)


async def _fallback_tool(hass: HomeAssistant, assist_name: str) -> llm.Tool | None:
    for module_name, class_name, arguments in _FALLBACK_CLASSES.get(assist_name, ()):
        try:
            module = await async_import_module(hass, module_name)
        except ImportError:
            continue
        if (tool_class := getattr(module, class_name, None)) is not None:
            tool: llm.Tool = tool_class(*arguments)
            return tool
    return None


async def _assist_tool(
    hass: HomeAssistant, assist_name: str, api_tools: list[llm.Tool]
) -> llm.Tool | None:
    """The Assist tool itself, or None if this Home Assistant version lacks it.

    Plain intents are read from the intent registry rather than the Assist API's
    per-conversation list, which shrinks and grows with the Expose list and between versions.
    The Tools stay fixed, and exposure is enforced when the tool matches its targets.
    """
    if assist_name in _API_NAMES:
        return find_api_tool(api_tools, assist_name) or await _fallback_tool(hass, assist_name)
    handler = next((h for h in intent.async_get(hass) if h.intent_type == assist_name), None)
    return llm.IntentTool(assist_name, handler) if handler else None


def _version(definition: dict[str, Any]) -> str:
    """Changes with the definition: the Codie refuses a changed Tool under an old version."""
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
    return f"{TOOL_VERSION}-{digest[:8]}"


async def _offered_actions(
    hass: HomeAssistant, tool: CodieTool, api_tools: list[llm.Tool]
) -> dict[str, str]:
    """The actions of `tool` whose Assist intent exists, with that intent's name."""
    return {
        action: assist_name
        for action, assist_name in tool.actions.items()
        if await _assist_tool(hass, assist_name, api_tools) is not None
    }


async def _assist_definition(
    hass: HomeAssistant,
    tool: CodieTool,
    api_tools: list[llm.Tool],
    serializer: Callable[[Any], Any],
) -> tuple[str, dict[str, Any]]:
    """The description and parameters of a Tool backed by Assist.

    Raises `ToolLeftOut` if the Tool cannot be offered.
    """
    actions = await _offered_actions(hass, tool, api_tools)
    assist_name = next(iter(actions.values())) if actions else tool.assist_name
    if (assist_tool := await _assist_tool(hass, assist_name, api_tools)) is None:
        _LOGGER.debug("Assist tool %s is not available; skipping %s", assist_name, tool.name)
        raise ToolLeftOut(f"Assist tool {assist_name} is not available")
    description, parameters = assist_tool.description or "", assist_tool.parameters
    try:
        schema = from_probatio(parameters, serializer)
        for name in tool.hidden:
            schema["properties"].pop(name, None)
        if tool.actions:
            schema["properties"]["action"] = {"type": "string", "enum": list(actions)}
            schema.setdefault("required", []).append("action")
    except SchemaLimitError as err:
        _LOGGER.warning("Skipping tool %s: %s", tool.name, err)
        raise ToolLeftOut(str(err)) from err
    if tool.scripts_intro and (scripts := _script_names(hass)):
        description = _list_names(tool.scripts_intro, scripts)
    return description, schema


class ToolLeftOut(Exception):
    """A Tool cannot be offered; the message says why."""


async def _build_tools(
    hass: HomeAssistant, language: str | None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The Tool definitions, and the Tools left out, each with the reason.

    `language` is the Codie's AI language, which decides whether the Timer Tools are offered.
    """
    api_tools, serializer = await _assist_api_tools(hass)
    tools: list[dict[str, Any]] = []
    left_out: dict[str, str] = {}
    for tool in TOOLS:
        try:
            if tool.parameters is None:
                description, schema = await _assist_definition(hass, tool, api_tools, serializer)
            elif timers_supported(hass, language):
                description, schema = "", copy.deepcopy(dict(tool.parameters))
            else:
                _LOGGER.debug("Timers are unavailable; skipping %s", tool.name)
                raise ToolLeftOut(tool.unavailable or "Timers are unavailable")
        except ToolLeftOut as err:
            left_out[tool.name] = str(err)
            continue
        definition = {
            "name": tool.name,
            "description": (tool.description or description)[:MAX_DESCRIPTION_CHARS],
            "parameters": schema,
            "result_schema": _ASSIST_RESULT_SCHEMA,
            "risk": tool.risk,
            "idempotent": tool.risk == "read",
            "confirmation_required": False,
            "timeout_ms": DEFAULT_TIMEOUT_MS,
        }
        tools.append({**definition, "version": _version(definition)})
    return tools, left_out


def _catalog(tools: list[dict[str, Any]], expected_revision: int) -> dict[str, Any]:
    """The body for `PUT /data/user/tool-integrations/{id}`."""
    return {
        "protocol_version": "v1",
        "expected_revision": expected_revision,
        "name": "Home Assistant",
        "enabled": True,
        "tools": tools,
    }


@dataclass(frozen=True)
class RegisteredCatalog:
    """The catalog the Codie last accepted."""

    tools: list[dict[str, Any]]
    # The Tools that were left out, each with the reason.
    left_out: dict[str, str]
    # The revision the Codie gave it; None if its reply did not say.
    revision: int | None


async def async_register_catalog(
    hass: HomeAssistant, client: CodieClient, integration_id: str, language: str | None
) -> RegisteredCatalog:
    """Create or update the catalog and return what the Codie now holds.

    On a revision conflict the current revision is read again and the PUT retried.
    """
    attempt = 0
    while True:
        attempt += 1
        current = await client.async_get_tool_integration(integration_id)
        revision = int(current["revision"]) if current else 0
        tools, left_out = await _build_tools(hass, language)
        try:
            reply = await client.async_put_tool_integration(
                integration_id, _catalog(tools, revision)
            )
        except CodieApiError as err:
            if err.status != 409 or attempt >= REGISTER_ATTEMPTS:
                raise
            _LOGGER.debug("Catalog revision conflict, re-reading (attempt %s)", attempt)
            continue
        new_revision = reply.get("revision")
        valid = isinstance(new_revision, int) and not isinstance(new_revision, bool)
        return RegisteredCatalog(tools, left_out, new_revision if valid else None)


class CatalogRegistrar:
    """Keeps the Codie's catalog in step with the Assist tools Home Assistant has."""

    def __init__(
        self, hass: HomeAssistant, client: CodieClient, integration_id: str, language: str | None
    ) -> None:
        self._hass = hass
        self._client = client
        self._integration_id = integration_id
        self._language = language
        self.catalog: RegisteredCatalog | None = None
        self._lock = asyncio.Lock()

    async def async_register(self) -> None:
        """Register the catalog unconditionally."""
        async with self._lock:
            self.catalog = await async_register_catalog(
                self._hass, self._client, self._integration_id, self._language
            )

    async def async_refresh(self) -> bool:
        """Register again if the available Tools changed. Returns whether it did."""
        async with self._lock:
            tools, left_out = await _build_tools(self._hass, self._language)
            if self.catalog is not None and tools == self.catalog.tools:
                # The same Tools, but a Tool may be left out for another reason now.
                self.catalog = dataclasses.replace(self.catalog, left_out=left_out)
                return False
            self.catalog = await async_register_catalog(
                self._hass, self._client, self._integration_id, self._language
            )
            return True


def find_tool(name: str) -> CodieTool | None:
    """The Tool called `name`, or None."""
    return next((tool for tool in TOOLS if tool.name == name), None)


async def async_call_tool(
    hass: HomeAssistant,
    language: str | None,
    timers: CodieTimers,
    name: str,
    arguments: dict[str, Any],
    affected: list[str] | None = None,
) -> dict[str, Any]:
    """Run a Tool and return the result data; Timer Tools go to `timers`, the rest to Assist.

    The ids of the entities the call acted on are added to `affected`, for the record of the call.
    """
    tool = find_tool(name)
    if tool is None:
        raise ToolError(ERROR_UNKNOWN_TOOL, f"No tool named {name}")
    if tool.parameters is not None:
        if not timers_supported(hass, language):
            raise ToolError(ERROR_TOOL_UNAVAILABLE, tool.unavailable or f"{name} is unavailable")
        if name == "timer_start":
            return await timers.async_start(arguments)
        return await timers.async_control(arguments)
    context = _llm_context(language)
    arguments = dict(arguments)
    assist_name = tool.assist_name_for(arguments)
    if assist_name is None:
        raise ToolError(
            ERROR_INVALID_ARGUMENTS,
            f"action must be one of {', '.join(tool.actions)}",
        )
    arguments.pop("action", None)
    try:
        api_tools, _ = await _assist_api_tools(hass, language)
        assist_tool = await _assist_tool(hass, assist_name, api_tools)
        if assist_tool is None or (
            assist_name in _API_NAMES and find_api_tool(api_tools, assist_name) is None
        ):
            raise ToolError(
                ERROR_TOOL_UNAVAILABLE,
                tool.unavailable
                or f"{assist_name} is not available; no exposed entity supports it",
            )
        raw = await assist_tool.async_call(
            hass, llm.ToolInput(assist_tool.name, _visible(tool, arguments)), context
        )
    except intent.MatchFailedError as err:
        reason = err.result.no_match_reason
        if reason == intent.MatchFailedReason.DUPLICATE_NAME:
            raise _duplicate_name_error(hass, err) from err
        if reason == intent.MatchFailedReason.MULTIPLE_TARGETS:
            raise ToolNeedsClarification(
                ERROR_AMBIGUOUS_TARGET,
                f"More than one exposed entity matches {err.result.no_match_name or 'that'}",
            ) from err
        raise ToolError(
            ERROR_TARGET_NOT_FOUND,
            f"No exposed entity or area matches that request ({reason.name if reason else '?'}). "
            f"Retry with one of these exposed names: {_exposed_names(hass)}",
        ) from err
    except (intent.InvalidSlotInfo, probatio.Invalid) as err:
        raise ToolError(ERROR_INVALID_ARGUMENTS, f"Invalid arguments for {name}") from err
    except intent.IntentError as err:
        raise ToolError(
            ERROR_INTENT_FAILED, str(err) or "Home Assistant could not do that"
        ) from err
    except HomeAssistantError as err:
        raise ToolError(ERROR_HOME_ASSISTANT, str(err)) from err
    data = _shape_result(raw)
    if affected is not None:
        affected.extend(_acted_on(raw))
    return data


def _duplicate_name_error(hass: HomeAssistant, err: intent.MatchFailedError) -> ToolError:
    """Ask which one only when their areas tell them apart; otherwise say how to fix it.

    A TV seen through Google Cast and through its own integration is two "Jay TV"s in one
    room. Asking which one gets the answer "Jay TV" again, so the call could never succeed.
    """
    name = err.result.no_match_name or "that"
    constraints = dataclasses.replace(err.constraints, allow_duplicate_names=True)
    states = intent.async_match_targets(hass, constraints).states
    areas = [_area_name(hass, state.entity_id) for state in states]
    if None not in areas and len(set(areas)) == len(areas):
        # The name the user said, not each state's name: Assist matched it by alias, so a
        # friendly name could miss on the retry.
        choices = ", ".join(sorted(f"{name} ({area})" for area in areas))
        return ToolNeedsClarification(
            ERROR_AMBIGUOUS_TARGET,
            f"More than one exposed entity matches {name}: {choices}. Ask which area.",
        )
    entity_ids = ", ".join(sorted(state.entity_id for state in states))
    return ToolError(
        ERROR_AMBIGUOUS_TARGET,
        f"{len(states)} exposed entities are named {name} ({entity_ids}) and their areas do "
        "not tell them apart. Tell the user to give them different names or aliases in Home "
        "Assistant, or expose only one of them to Assist.",
    )


def _area_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """The name of the entity's area, or of its device's area if it has none of its own."""
    area_id = None
    if entry := er.async_get(hass).async_get(entity_id):
        device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
        area_id = entry.area_id or (device.area_id if device else None)
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    return area.name if area else None


def _entity_names(hass: HomeAssistant, state: State) -> list[str]:
    """The names Assist matches an entity by: its aliases, or its entity id when it has none."""
    entry = er.async_get(hass).async_get(state.entity_id)
    return intent.async_get_entity_aliases(hass, entry, state=state) or [state.entity_id]


def _exposed_names(hass: HomeAssistant) -> str:
    """The names of the entities Assist may use, with their areas, cut to fit an error.

    These are the names Assist matches: an entity's aliases, which hold its name unless the
    user removed it, or its entity id when it has none.
    """
    names: list[str] = []
    for state in hass.states.async_all():
        if not async_should_expose(hass, "conversation", state.entity_id):
            continue
        area = _area_name(hass, state.entity_id)
        for name in _entity_names(hass, state):
            names.append(f"{name} ({area})" if area else name)
    text = ", ".join(sorted(names))
    return text if len(text) <= MAX_NAMES_CHARS else text[:MAX_NAMES_CHARS] + ", ..."


def _script_names(hass: HomeAssistant) -> list[str]:
    """The names Assist matches the exposed scripts by."""
    return sorted(
        name
        for state in hass.states.async_all("script")
        if async_should_expose(hass, "conversation", state.entity_id)
        for name in _entity_names(hass, state)
    )


def _list_names(intro: str, names: list[str]) -> str:
    """`intro` and as many whole `names` as fit a description, then ", ..." if some do not."""
    for count in range(len(names), 0, -1):
        text = intro + ", ".join(names[:count]) + (", ..." if count < len(names) else "")
        if len(text) <= MAX_DESCRIPTION_CHARS:
            return text
    return intro + "..."


def _visible(tool: CodieTool, arguments: dict[str, Any]) -> dict[str, Any]:
    """`arguments` without the parameters this Tool hides."""
    return {key: value for key, value in arguments.items() if key not in tool.hidden}


def failure_data(message: str) -> dict[str, Any]:
    """The result data that goes with a `failed` or `needs_clarification` status."""
    return {"success": False, "error": message[:MAX_ERROR_CHARS]}


def _unwrap(raw: Any) -> tuple[dict[str, Any], bool]:
    """The result dict of an Assist result and whether the tool reported a failure."""
    if isinstance(raw, dict):
        return raw, False
    return raw.data, bool(getattr(raw, "error", False))


def _shape_result(raw: Any) -> dict[str, Any]:
    """Reduce an Assist result to the stable shape of `_ASSIST_RESULT_SCHEMA`.

    Older Home Assistant returns a dict. 2026.11 returns a `ToolResult` whose `data` is that
    dict, with `error` set when the tool failed.
    """
    raw, failed = _unwrap(raw)
    if "response_type" in raw:
        return _shape_intent_response(raw)
    if failed or not raw.get("success", "error" not in raw):
        raise ToolError(ERROR_ASSIST_FAILED, str(raw.get("error") or "Home Assistant failed"))
    data: dict[str, Any] = {"success": True}
    for key in ("result", "items"):
        if key in raw:
            value = raw[key]
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            data["result"] = _truncate(text, MAX_RESULT_BYTES)
            break
    return data


def _acted_on(raw: Any) -> list[str]:
    """The ids of the entities an intent response says it acted on."""
    raw, _ = _unwrap(raw)
    if "response_type" not in raw:
        return []
    done = (raw.get("data") or {}).get("success", [])
    return [t["id"] for t in done if t.get("type") == "entity" and t.get("id")]


def _shape_intent_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce an intent response (what the Hass* tools return) to the stable shape."""
    speech = ((raw.get("speech") or {}).get("plain") or {}).get("speech") or ""
    details = raw.get("data") or {}
    done = [t["name"] for t in details.get("success", []) if t.get("name")]
    failed = [t["name"] for t in details.get("failed", []) if t.get("name")]
    if raw["response_type"] == "error":
        raise ToolError(
            str(details.get("code") or ERROR_ASSIST_FAILED),
            speech or "Home Assistant could not do that",
        )
    text = speech or (f"Done: {', '.join(done)}" if done else "Done")
    if failed:
        text += f" (failed: {', '.join(failed)})"
    return {"success": True, "result": _truncate(text, MAX_RESULT_BYTES)}


def _truncate(text: str, max_bytes: int) -> str:
    """Cut `text` to at most `max_bytes` of UTF-8 without splitting a character."""
    return text.encode()[:max_bytes].decode(errors="ignore")
