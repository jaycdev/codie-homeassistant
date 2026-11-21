"""The Tools the Codie's Character can call, each backed by an Assist LLM API tool."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .api import CodieApiError, CodieClient
from .errors import ToolError

_LOGGER = logging.getLogger(__name__)

TOOL_VERSION = "v1"
DEFAULT_TIMEOUT_MS = 10000
# The Codie accepts at most 16 KiB of result data.
MAX_RESULT_BYTES = 12000
MAX_ERROR_CHARS = 1000
REGISTER_ATTEMPTS = 3

ERROR_UNKNOWN_TOOL = "unknown_tool"
ERROR_HOME_ASSISTANT = "home_assistant_error"

_EMPTY_OBJECT: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

# Assist tools return {"success": bool, "result": str} or {"success": False, "error": str}.
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


@dataclass(frozen=True)
class CodieTool:
    """One Tool offered to the Character and the Assist tool that runs it."""

    name: str
    assist_name: str
    risk: str
    description: str
    parameters: dict[str, Any]
    result_schema: dict[str, Any]


TOOLS: tuple[CodieTool, ...] = (
    CodieTool(
        name="get_home_state",
        assist_name="GetLiveContext",
        risk="read",
        description=(
            "Get the current state of the user's exposed smart home devices, sensors and areas. "
            "Call it to answer questions about the home, and whenever you are unsure of a "
            "device name or its state."
        ),
        parameters=_EMPTY_OBJECT,
        result_schema=_ASSIST_RESULT_SCHEMA,
    ),
)


def build_catalog(expected_revision: int) -> dict[str, Any]:
    """The body for `PUT /data/user/tool-integrations/{id}`."""
    return {
        "protocol_version": "v1",
        "expected_revision": expected_revision,
        "name": "Home Assistant",
        "enabled": True,
        "tools": [
            {
                "name": tool.name,
                "version": TOOL_VERSION,
                "description": tool.description,
                "parameters": tool.parameters,
                "result_schema": tool.result_schema,
                "risk": tool.risk,
                "idempotent": tool.risk == "read",
                "confirmation_required": False,
                "timeout_ms": DEFAULT_TIMEOUT_MS,
            }
            for tool in TOOLS
        ],
    }


async def async_register_catalog(client: CodieClient, integration_id: str) -> int:
    """Create or update the catalog and return its new revision.

    On a revision conflict the current revision is read again and the PUT retried.
    """
    attempt = 0
    while True:
        attempt += 1
        current = await client.async_get_tool_integration(integration_id)
        revision = int(current["revision"]) if current else 0
        try:
            result = await client.async_put_tool_integration(
                integration_id, build_catalog(revision)
            )
        except CodieApiError as err:
            if err.status != 409 or attempt >= REGISTER_ATTEMPTS:
                raise
            _LOGGER.debug("Catalog revision conflict, re-reading (attempt %s)", attempt)
            continue
        return int(result["revision"])


async def async_call_tool(
    hass: HomeAssistant, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Run a Tool through Home Assistant's Assist API and return the result data."""
    tool = next((t for t in TOOLS if t.name == name), None)
    if tool is None:
        raise ToolError(ERROR_UNKNOWN_TOOL, f"No tool named {name}")
    # The Assist API only ever sees entities exposed to the conversation assistant.
    context = llm.LLMContext(
        platform="codie",
        context=None,
        language=None,
        assistant="conversation",
        device_id=None,
    )
    try:
        api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)
        raw = await api.async_call_tool(llm.ToolInput(tool.assist_name, arguments))
    except HomeAssistantError as err:
        raise ToolError(ERROR_HOME_ASSISTANT, str(err)) from err
    return _shape_result(raw)


def _shape_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce an Assist result to the stable shape of `_ASSIST_RESULT_SCHEMA`."""
    data: dict[str, Any] = {"success": bool(raw.get("success", "error" not in raw))}
    if "result" in raw:
        data["result"] = _truncate(str(raw["result"]), MAX_RESULT_BYTES)
    if raw.get("error"):
        data["error"] = str(raw["error"])[:MAX_ERROR_CHARS]
    return data


def _truncate(text: str, max_bytes: int) -> str:
    """Cut `text` to at most `max_bytes` of UTF-8 without splitting a character."""
    return text.encode()[:max_bytes].decode(errors="ignore")
