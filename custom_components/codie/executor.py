"""The executor WebSocket: receives `invoke` messages and answers each with a `result`."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiohttp import ClientError, ClientWebSocketResponse, WSMsgType

from .api import CodieAuthError, CodieClient, CodieError
from .errors import ToolError
from .tools import DEFAULT_TIMEOUT_MS, failure_data

_LOGGER = logging.getLogger(__name__)

# Answer slightly before the Codie's deadline so the result still counts.
DEADLINE_MARGIN_S = 0.5
READY_TIMEOUT_S = 10
# A connection that stayed up this long was healthy, so the next drop starts the backoff over.
STABLE_AFTER_S = 30
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
ERROR_TIMEOUT = "timeout"
ERROR_INTERNAL = "internal_error"

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# Told the Tool, its arguments and the code it failed with (None if it succeeded) once a call ends.
CallObserver = Callable[[str, dict[str, Any], str | None], None]


class CodieExecutor:
    """Holds one executor connection, keeps it alive, and tracks whether it is `ready`."""

    def __init__(
        self,
        client: CodieClient,
        integration_id: str,
        handler: ToolHandler,
        on_auth_failed: Callable[[], None] | None = None,
        on_call: CallObserver | None = None,
    ) -> None:
        self._client = client
        self._on_auth_failed = on_auth_failed
        self._integration_id = integration_id
        self._handler = handler
        self._on_call = on_call
        self._ws: ClientWebSocketResponse | None = None
        # Running calls and their invocation ids, for the log if the socket dies under them.
        self._calls: dict[asyncio.Task[None], str] = {}
        self._listeners: set[Callable[[], None]] = set()
        self._closing = False
        self._reconnect_now = False
        self._was_ready = False
        self._wake = asyncio.Event()
        self.connected = False

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` whenever `connected` changes. Returns the remover."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    async def async_run(self) -> None:
        """Keep a connection up, reconnecting with backoff, until `async_close`."""
        backoff = INITIAL_BACKOFF_S
        failures = 0
        while not self._closing:
            started = time.monotonic()
            self._was_ready = False
            try:
                await self._serve()
            except CodieAuthError as err:
                # Reconnecting with credentials the Codie rejects cannot work; reauth takes over.
                _LOGGER.warning("The Codie no longer accepts our credentials: %s", err)
                if self._on_auth_failed is not None:
                    self._on_auth_failed()
                return
            except (CodieError, ClientError, OSError, TimeoutError, TypeError, ValueError) as err:
                # Warn once per outage; a Codie that is off for days must not flood the log.
                log = _LOGGER.warning if failures == 0 else _LOGGER.debug
                log("Executor connection failed: %s", err)
                failures += 1
            if self._was_ready:
                failures = 0
            if self._closing:
                return
            if self._reconnect_now:
                # Asked for by us (a changed catalog), so there is nothing to wait out.
                self._reconnect_now = False
                backoff = INITIAL_BACKOFF_S
                continue
            if time.monotonic() - started >= STABLE_AFTER_S:
                backoff = INITIAL_BACKOFF_S
            _LOGGER.debug("Executor reconnecting in %.1f s", backoff)
            await self._backoff(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def async_close(self) -> None:
        """Stop reconnecting and close the socket; `async_run` then returns."""
        self._closing = True
        self._wake.set()
        if self._ws is not None:
            await self._ws.close()

    async def async_reconnect(self) -> None:
        """Reconnect now, because the catalog changed and the Codie reads it per connection."""
        self._reconnect_now = True
        self._wake.set()
        if self._ws is not None:
            await self._ws.close()

    async def _backoff(self, delay: float) -> None:
        """Wait `delay` seconds, or less if a reconnect or close is requested."""
        self._wake.clear()
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)
        self._reconnect_now = False

    async def _serve(self) -> None:
        """One connection: connect, wait for `ready`, and serve invokes until it closes."""
        ws = await self._client.async_connect_executor(self._integration_id)
        self._ws = ws
        try:
            if self._reconnect_now:
                # The catalog changed while this connection was being made.
                return
            ready = await ws.receive_json(timeout=READY_TIMEOUT_S)
            if ready.get("type") != "ready":
                raise CodieError(f"Expected ready, got {ready.get('type')}")
            _LOGGER.debug("Executor ready for %s", self._integration_id)
            self._was_ready = True
            self._set_connected(True)
            async for message in ws:
                if message.type is not WSMsgType.TEXT:
                    continue
                try:
                    payload = message.json()
                except ValueError:
                    _LOGGER.warning("Ignoring an executor message that is not JSON")
                    continue
                if not self._dispatch(ws, payload):
                    break
        finally:
            self._set_connected(False)
            self._abandon_calls()
            self._ws = None
            await ws.close()

    def _abandon_calls(self) -> None:
        """Stop calls whose socket is gone. The Codie has given up on them too."""
        for task, invocation_id in self._calls.items():
            _LOGGER.warning(
                "Invocation %s was lost when the executor disconnected; its outcome is "
                "unknown and it will not be retried",
                invocation_id,
            )
            task.cancel()
        self._calls.clear()

    def _set_connected(self, connected: bool) -> None:
        if self.connected != connected:
            self.connected = connected
            for listener in list(self._listeners):
                listener()

    def _dispatch(self, ws: ClientWebSocketResponse, message: Any) -> bool:
        """Handle one message. Returns False if the connection should be dropped."""
        if not isinstance(message, dict):
            return True
        kind = message.get("type")
        if kind == "invoke":
            call = message.get("data")
            if not isinstance(call, dict) or not {"invocation_id", "name"} <= call.keys():
                _LOGGER.warning("Ignoring malformed invoke message")
                return True
            _LOGGER.debug(
                "Invoke %s(%s) timeout_ms=%s turn_id=%s",
                call["name"],
                call.get("arguments") or {},
                call.get("timeout_ms"),
                call.get("turn_id"),
            )
            task = asyncio.create_task(self._invoke(ws, call))
            self._calls[task] = str(call["invocation_id"])
            task.add_done_callback(lambda done: self._calls.pop(done, None))
        elif kind == "error":
            data = message.get("data")
            data = data if isinstance(data, dict) else {}
            _LOGGER.warning(
                "Executor protocol error %s: %s", data.get("code"), data.get("message") or ""
            )
            # An error about one invocation leaves the connection fine. Any other means
            # the two sides disagree about the connection itself, so start over.
            return "invocation_id" in data
        return True

    async def _invoke(self, ws: ClientWebSocketResponse, call: dict[str, Any]) -> None:
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
        if self._on_call is not None:
            self._on_call(call["name"], call.get("arguments") or {}, result.get("code"))
        if result["status"] == "succeeded":
            _LOGGER.debug(
                "Tool %s succeeded, result %d bytes: %.300s",
                call["name"],
                len(json.dumps(result["data"], ensure_ascii=False).encode()),
                result["data"],
            )
        else:
            _LOGGER.warning(
                "Tool %s(%s) %s: %s %s turn_id=%s",
                call["name"],
                call.get("arguments") or {},
                result["status"],
                result.get("code"),
                result.get("message") or result["data"].get("error", ""),
                call.get("turn_id"),
            )
        # Answer on the socket the call came in on: a newer connection knows nothing of it.
        try:
            await ws.send_json({"type": "result", "data": result})
        except ClientError, ConnectionError:
            _LOGGER.warning(
                "Invocation %s finished but the executor disconnected before the result was "
                "sent; its outcome is unknown to the Codie and it will not be retried",
                call["invocation_id"],
            )
