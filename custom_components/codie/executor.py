"""The executor WebSocket: receives `invoke` messages and answers each with a `result`."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import ClientWebSocketResponse, WSMsgType

from .api import CodieClient, CodieError
from .errors import ToolError
from .tools import DEFAULT_TIMEOUT_MS, failure_data

_LOGGER = logging.getLogger(__name__)

# Answer slightly before the Codie's deadline so the result still counts.
DEADLINE_MARGIN_S = 0.5
READY_TIMEOUT_S = 10
ERROR_TIMEOUT = "timeout"
ERROR_INTERNAL = "internal_error"

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class CodieExecutor:
    """Holds one executor connection and tracks whether it is `ready`."""

    def __init__(self, client: CodieClient, integration_id: str, handler: ToolHandler) -> None:
        self._client = client
        self._integration_id = integration_id
        self._handler = handler
        self._ws: ClientWebSocketResponse | None = None
        self._calls: set[asyncio.Task[None]] = set()
        self._listeners: set[Callable[[], None]] = set()
        self.connected = False

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` whenever `connected` changes. Returns the remover."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    async def async_run(self) -> None:
        """Connect, wait for `ready`, and serve invokes until the socket closes."""
        try:
            self._ws = await self._client.async_connect_executor(self._integration_id)
            ready = await self._ws.receive_json(timeout=READY_TIMEOUT_S)
            if ready.get("type") != "ready":
                raise CodieError(f"Expected ready, got {ready.get('type')}")
            _LOGGER.debug("Executor ready for %s", self._integration_id)
            self._set_connected(True)
            async for message in self._ws:
                if message.type is WSMsgType.TEXT:
                    self._dispatch(message.json())
        except (CodieError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Executor connection failed: %s", err)
        finally:
            self._set_connected(False)
            for task in self._calls:
                task.cancel()

    async def async_close(self) -> None:
        """Close the socket; `async_run` then returns."""
        if self._ws is not None:
            await self._ws.close()

    def _set_connected(self, connected: bool) -> None:
        if self.connected != connected:
            self.connected = connected
            for listener in list(self._listeners):
                listener()

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "invoke":
            call = message.get("data")
            if not isinstance(call, dict) or not {"invocation_id", "name"} <= call.keys():
                _LOGGER.warning("Ignoring malformed invoke message")
                return
            _LOGGER.debug(
                "Invoke %s(%s) timeout_ms=%s",
                call["name"],
                call.get("arguments") or {},
                call.get("timeout_ms"),
            )
            task = asyncio.create_task(self._invoke(call))
            self._calls.add(task)
            task.add_done_callback(self._calls.discard)
        elif kind == "error":
            _LOGGER.warning("Executor protocol error: %s", message.get("data", {}).get("code"))

    async def _invoke(self, call: dict[str, Any]) -> None:
        result: dict[str, Any] = {"invocation_id": call["invocation_id"]}
        # Relative to arrival, so a skewed clock on either side cannot shift the deadline.
        remaining = call.get("timeout_ms", DEFAULT_TIMEOUT_MS) / 1000 - DEADLINE_MARGIN_S
        try:
            async with asyncio.timeout(max(remaining, 0)):
                result["data"] = await self._handler(call["name"], call.get("arguments") or {})
            result["status"] = "succeeded"
        except TimeoutError:
            result.update(status="failed", code=ERROR_TIMEOUT, data=failure_data("Timed out"))
        except ToolError as err:
            result.update(
                status=err.status,
                code=err.code,
                message=str(err)[:500],
                data=failure_data(str(err)),
            )
        except Exception:
            _LOGGER.exception("Tool %s crashed", call["name"])
            result.update(status="failed", code=ERROR_INTERNAL, data=failure_data("Internal error"))
        if result["status"] == "succeeded":
            _LOGGER.debug(
                "Tool %s succeeded, result %d bytes: %.300s",
                call["name"],
                len(json.dumps(result["data"], ensure_ascii=False).encode()),
                result["data"],
            )
        else:
            _LOGGER.warning(
                "Tool %s(%s) %s: %s %s",
                call["name"],
                call.get("arguments") or {},
                result["status"],
                result.get("code"),
                result.get("message") or result["data"].get("error", ""),
            )
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json({"type": "result", "data": result})
