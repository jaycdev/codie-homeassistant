"""Records every Tool call as an event, for automations and the logbook."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .tools import find_tool

if TYPE_CHECKING:
    from . import CodieConfigEntry

_LOGGER = logging.getLogger(__name__)

# Fired for every Tool call, the read Tools included.
EVENT_TOOL_CALLED = f"{DOMAIN}_tool_called"
# Fired with the same data for the calls the logbook shows. The logbook cannot leave out single
# events of one type, and the read Tools would flood it, so it describes this one instead.
EVENT_TOOL_LOGGED = f"{DOMAIN}_tool_logged"

# What `async_call_tool` takes after the Tool's name and arguments.
RunTool = Callable[[str, dict[str, Any], list[str], dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolCallData(TypedDict):
    """The data of `codie_tool_called`, documented in the README."""

    device_id: str | None
    tool: str
    assist_tool: str | None
    arguments: dict[str, Any]
    success: bool
    error: str | None
    entity_ids: list[str]


class ToolLoggedData(ToolCallData):
    """The data of `codie_tool_logged`: that of the call, and the entity the logbook links."""

    # The logbook matches an entity's history on a single `entity_id` string, never a list.
    entity_id: NotRequired[str]


# The entities the running call acted on. Each call runs in its own task, and the executor reports
# the outcome in that task, so this is the call's own list.
_affected: ContextVar[list[str]] = ContextVar("codie_affected_entity_ids")
# The arguments the integration added to the running call, such as the Codie's area.
_added: ContextVar[dict[str, Any]] = ContextVar("codie_added_arguments")


class ToolRecorder:
    """Wraps the Tool runner to collect the entities a call acted on, and records its outcome."""

    def __init__(self, hass: HomeAssistant, entry: CodieConfigEntry, run: RunTool) -> None:
        self._hass = hass
        self._entry = entry
        self._run = run

    async def async_handle(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run the Tool, noting the entities it acted on and the arguments it added for `record`."""
        affected: list[str] = []
        added: dict[str, Any] = {}
        _affected.set(affected)
        _added.set(added)
        return await self._run(name, arguments, affected, added)

    def record(self, name: str, arguments: dict[str, Any], error: str | None) -> None:
        """Fire the events for a finished call. `error` is the code it failed with, if it did.

        Recording must never affect the call, so a failure here is only logged.
        """
        try:
            self._fire(name, arguments, error)
        except Exception:
            _LOGGER.exception("Could not record the call of %s", name)

    def _affected_entities(self) -> list[str]:
        return list(_affected.get([]))

    def _fire(self, name: str, arguments: Any, error: str | None) -> None:
        # The Codie sends whatever it likes, so anything but an object counts as no arguments.
        if not isinstance(arguments, dict):
            arguments = {}
        # The arguments that ran, which is what an automation needs to see.
        arguments = {**arguments, **_added.get({})}
        tool = find_tool(name)
        device = dr.async_get(self._hass).async_get_device_by_identifier(
            (DOMAIN, str(self._entry.unique_id)), self._entry.entry_id
        )
        data = ToolCallData(
            device_id=device.id if device else None,
            tool=name,
            assist_tool=tool.assist_name_for(arguments) if tool else None,
            arguments=arguments,
            success=error is None,
            error=error,
            entity_ids=self._affected_entities(),
        )
        self._hass.bus.async_fire(EVENT_TOOL_CALLED, data)
        # An unknown Tool did nothing, and a read has no place in the logbook.
        if tool is not None and not tool.reads(arguments):
            logged = cast(ToolLoggedData, dict(data))
            if len(data["entity_ids"]) == 1:
                logged["entity_id"] = data["entity_ids"][0]
            self._hass.bus.async_fire(EVENT_TOOL_LOGGED, logged)
