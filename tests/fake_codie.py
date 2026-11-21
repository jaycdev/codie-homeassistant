"""A fake Codie: an aiohttp test server speaking the parts of the 1.6.0 HTTP API we use.

Every signed request is checked by recomputing the HMAC from the canonical string
(guide section 4), so a client that signs wrongly is rejected exactly like on a real Codie.
Later issues extend this harness with more endpoints.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

API = "/v1/api"
CLOCK_TOLERANCE_SECONDS = 300
PAIRING_VALIDITY_SECONDS = 300
PUBLIC_PATHS = frozenset(
    f"{API}{path}"
    for path in (
        "/auth/ping",
        "/auth/bootstrap",
        "/auth/pair/request",
        "/auth/pair/confirm",
        "/auth/pair/cancel",
    )
)
WS_PREFIX = "/v1/ws/"
WS_QUERY_HEADERS = {
    "sig": "X-Local-Sig",
    "timestamp": "X-Local-Timestamp",
    "nonce": "X-Local-Nonce",
    "payload_mode": "X-Local-Payload-Mode",
    "body_hash": "X-Local-Body-Hash",
    "user_id": "X-Local-User-Id",
    "session_id": "X-Local-Session-Id",
}
SIGNED_HEADERS = (
    "X-Local-Sig",
    "X-Local-Timestamp",
    "X-Local-Nonce",
    "X-Local-Payload-Mode",
    "X-Local-Body-Hash",
    "X-Local-User-Id",
)


def canonical_string(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    payload_mode: str,
    body_hash: str,
    user_id: str,
) -> str:
    """The string the guide says to sign, written out independently of the client."""
    return "|".join((method, path, timestamp, nonce, payload_mode, body_hash, user_id))


def sign(secret: str, canonical: str) -> str:
    """HMAC-SHA256 over the UTF-8 canonical string, keyed by the raw secret string."""
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


@dataclass
class RecordedRequest:
    """One request as the fake saw it."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    raw_path: str = ""
    rejected_with: str | None = None

    @property
    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class FakeCodie:
    """Serves a fake Codie on localhost; use as `async with` or via `start`/`stop`."""

    machine_id: str = "a" * 64
    user_id: str = "11111111-2222-3333-4444-555555555555"
    username: str = "tester"
    local_secret: str = "fake-local-secret"
    local_session_id: str = ""
    pairing_code: str = "123456"
    logged_in: bool = True
    clock: Callable[[], float] = time.time

    requests: list[RecordedRequest] = field(default_factory=list)
    seen_nonces: set[str] = field(default_factory=set)
    # Replies to send instead of the normal handler: path suffix -> (status, raw body).
    overrides: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    confirm_scopes: list[str] = field(default_factory=list)
    cancelled_challenges: list[str] = field(default_factory=list)
    challenge_id: str | None = None
    challenge_state: str = "none"  # none | pending | expired | used | cancelled
    # Registered tool integrations: id -> {"revision": int, "catalog": dict}.
    tool_integrations: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleted_integrations: list[str] = field(default_factory=list)
    # Reject this many PUTs with 409 before accepting one, as if another writer raced us.
    put_conflicts: int = 0
    # Executor WebSocket state.
    executor_connects: int = 0
    executor_results: list[dict[str, Any]] = field(default_factory=list)
    _executor: web.WebSocketResponse | None = None
    _result_waiters: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _server: TestServer | None = None

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get(f"{API}/auth/ping", self._ping)
        app.router.add_get(f"{API}/auth/bootstrap", self._bootstrap)
        app.router.add_post(f"{API}/auth/pair/request", self._pair_request)
        app.router.add_post(f"{API}/auth/pair/confirm", self._pair_confirm)
        app.router.add_post(f"{API}/auth/pair/cancel", self._pair_cancel)
        app.router.add_get(f"{API}/data/user/system", self._system)
        tool_path = f"{API}/data/user/tool-integrations/{{integration_id}}"
        app.router.add_get(tool_path, self._tool_get)
        app.router.add_put(tool_path, self._tool_put)
        app.router.add_delete(tool_path, self._tool_delete)
        app.router.add_get(f"{WS_PREFIX}tool-integrations/{{integration_id}}/executor", self._ws)
        app.router.add_post(f"{API}/data/user/echo", self._echo)
        app.router.add_get(f"{API}/data/user/name/{{name}}", self._name)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()

    async def stop(self) -> None:
        # An open WebSocket would hold the server's shutdown for its full grace period.
        await self.drop_executor()
        if self._server is not None:
            await self._server.close()
            self._server = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        assert self._server is not None
        assert self._server.port is not None
        return self._server.port

    # --- test controls and views ---------------------------------------------------------

    def expire_challenge(self) -> None:
        """Make the displayed pairing code stop working, as after five minutes."""
        self.challenge_state = "expired"

    def signed_requests(self) -> list[RecordedRequest]:
        return [r for r in self.requests if r.path not in PUBLIC_PATHS]

    def requests_to(self, suffix: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.path == f"{API}{suffix}"]

    @property
    def executor_connected(self) -> bool:
        return self._executor is not None and not self._executor.closed

    async def invoke(
        self, integration_id: str, name: str, arguments: dict[str, Any], timeout_ms: int = 10000
    ) -> dict[str, Any]:
        """Send an `invoke` to the connected executor and return its `result` data."""
        assert self._executor is not None
        invocation_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._result_waiters[invocation_id] = future
        now_ms = int(self.clock() * 1000)
        await self._executor.send_json(
            {
                "type": "invoke",
                "data": {
                    "arguments": arguments,
                    "call_id": str(uuid.uuid4()),
                    "deadline_unix_ms": now_ms + timeout_ms,
                    "integration_id": integration_id,
                    "invocation_id": invocation_id,
                    "name": name,
                    "revision": self.tool_integrations[integration_id]["revision"],
                    "round_id": "r1",
                    "session_id": "s1",
                    "timeout_ms": timeout_ms,
                    "turn_id": "t1",
                    "version": "v1",
                },
            }
        )
        async with asyncio.timeout(5):
            return await future

    async def drop_executor(self) -> None:
        """Close the executor socket from the Codie's side."""
        if self._executor is not None:
            await self._executor.close()

    # --- signature verification ----------------------------------------------------------

    def _verify(self, request: web.Request, body: bytes) -> str | None:
        """Return the error code to reject with, or None if the request is valid."""
        headers: Any = request.headers
        if request.path.startswith(WS_PREFIX):
            # A WebSocket handshake carries the same fields as query parameters.
            headers = {
                header: request.query[key]
                for key, header in WS_QUERY_HEADERS.items()
                if key in request.query
            }
        if any(name not in headers for name in SIGNED_HEADERS):
            return "INVALID_SIGNATURE"
        if headers["X-Local-User-Id"] != self.user_id:
            return "LOCAL_USER_MISMATCH"
        if self.local_session_id and headers.get("X-Local-Session-Id") != self.local_session_id:
            return "LOCAL_SESSION_EXPIRED"
        if not self.local_session_id and "X-Local-Session-Id" in headers:
            return "LOCAL_SESSION_EXPIRED"

        try:
            timestamp = int(headers["X-Local-Timestamp"])
        except ValueError:
            return "REQUEST_EXPIRED"
        if abs(self.clock() - timestamp) > CLOCK_TOLERANCE_SECONDS:
            return "REQUEST_EXPIRED"

        mode = headers["X-Local-Payload-Mode"]
        if mode == "empty" and body:
            return "INVALID_SIGNATURE"
        if mode == "json" and not body:
            return "INVALID_SIGNATURE"
        if mode not in ("empty", "json"):
            return "INVALID_SIGNATURE"
        if headers["X-Local-Body-Hash"] != hashlib.sha256(body).hexdigest():
            return "INVALID_SIGNATURE"

        nonce = headers["X-Local-Nonce"]
        if not 1 <= len(nonce) <= 128 or "|" in nonce or "\n" in nonce:
            return "INVALID_SIGNATURE"

        canonical = canonical_string(
            request.method,
            request.path,
            headers["X-Local-Timestamp"],
            nonce,
            mode,
            headers["X-Local-Body-Hash"],
            headers["X-Local-User-Id"],
        )
        expected = sign(self.local_secret, canonical)
        if not hmac.compare_digest(expected, headers["X-Local-Sig"]):
            return "INVALID_SIGNATURE"

        # Only a correctly signed request burns its nonce.
        if nonce in self.seen_nonces:
            return "NONCE_REUSED"
        self.seen_nonces.add(nonce)
        return None

    @web.middleware
    async def _middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Any],
    ) -> web.StreamResponse:
        body = await request.read()
        recorded = RecordedRequest(
            request.method, request.path, dict(request.headers), body, request.raw_path
        )
        self.requests.append(recorded)
        if request.path not in PUBLIC_PATHS:
            error = self._verify(request, body)
            if error:
                recorded.rejected_with = error
                return _error(401, error)
        for suffix, (status, raw) in self.overrides.items():
            if request.path == f"{API}{suffix}":
                return web.Response(status=status, body=raw, content_type="application/json")
        response: web.StreamResponse = await handler(request)
        return response

    # --- handlers ------------------------------------------------------------------------

    async def _ping(self, request: web.Request) -> web.Response:
        return web.json_response({"success": True, "data": "pong"})

    async def _bootstrap(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "data": {
                    "user_id": self.user_id if self.logged_in else "",
                    "device_logged_in": self.logged_in,
                    "language": "en",
                    "ai_language": "en",
                },
            }
        )

    async def _pair_request(self, request: web.Request) -> web.Response:
        if not self.logged_in:
            return _error(403, "DEVICE_NOT_LOGGED_IN")
        self.challenge_id = str(uuid.uuid4())
        self.challenge_state = "pending"
        expires_at_ms = int((self.clock() + PAIRING_VALIDITY_SECONDS) * 1000)
        return web.json_response(
            {
                "success": True,
                "data": {"challenge_id": self.challenge_id, "expires_at_ms": expires_at_ms},
            }
        )

    async def _pair_confirm(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.confirm_scopes.append(payload.get("auth_scope", "permanent"))
        if payload.get("challenge_id") != self.challenge_id:
            return _error(400, "INVALID_REQUEST")
        if self.challenge_state == "expired":
            return _error(400, "PAIRING_CODE_EXPIRED")
        if self.challenge_state == "used":
            return _error(400, "PAIRING_CODE_USED")
        if self.challenge_state != "pending":
            return _error(400, "INVALID_REQUEST")
        if payload.get("pairing_code") != self.pairing_code:
            return _error(400, "INVALID_CODE")
        self.challenge_state = "used"
        return web.json_response(
            {
                "success": True,
                "data": {
                    "auth_scope": payload.get("auth_scope", "permanent"),
                    "challenge_id": self.challenge_id,
                    "device_id": self.machine_id,
                    "local_secret": self.local_secret,
                    "local_session_id": self.local_session_id,
                    "local_session_expires_at_ms": 0,
                    "name": "Codie",
                    "version": "261118175",
                },
            }
        )

    async def _pair_cancel(self, request: web.Request) -> web.Response:
        payload = await request.json()
        challenge_id = payload.get("challenge_id")
        self.cancelled_challenges.append(challenge_id)
        transitioned = challenge_id == self.challenge_id and self.challenge_state == "pending"
        if transitioned:
            self.challenge_state = "cancelled"
        return web.json_response({"success": True, "data": {"transitioned": transitioned}})

    async def _system(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "data": {
                    "machine_id": self.machine_id,
                    "user_id": self.user_id,
                    "username": self.username,
                    "language": "en",
                    "ai_language": "ja",
                    "timezone": "Europe/London",
                    "capabilities": {},
                },
            }
        )

    async def _tool_get(self, request: web.Request) -> web.Response:
        entry = self.tool_integrations.get(request.match_info["integration_id"])
        if entry is None:
            return _error(404, "NOT_FOUND")
        return web.json_response({"success": True, "data": self._tool_view(request, entry)})

    def _tool_view(self, request: web.Request, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            **entry["catalog"],
            "integration_id": request.match_info["integration_id"],
            "revision": entry["revision"],
            "online": self.executor_connected,
        }

    async def _tool_put(self, request: web.Request) -> web.Response:
        integration_id = request.match_info["integration_id"]
        catalog = await request.json()
        entry = self.tool_integrations.get(integration_id)
        current = entry["revision"] if entry else 0
        if self.put_conflicts > 0:
            self.put_conflicts -= 1
            if entry:
                entry["revision"] += 1
            return _error(409, "REVISION_CONFLICT")
        if catalog["expected_revision"] != current:
            return _error(409, "REVISION_CONFLICT")
        new = {"revision": current + 1, "catalog": catalog}
        self.tool_integrations[integration_id] = new
        return web.json_response({"success": True, "data": self._tool_view(request, new)})

    async def _tool_delete(self, request: web.Request) -> web.Response:
        integration_id = request.match_info["integration_id"]
        self.tool_integrations.pop(integration_id, None)
        self.deleted_integrations.append(integration_id)
        return web.Response(status=204)

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        integration_id = request.match_info["integration_id"]
        entry = self.tool_integrations.get(integration_id)
        if entry is None:
            return _error(404, "NOT_FOUND")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._executor = ws
        self.executor_connects += 1
        await ws.send_json(
            {
                "type": "ready",
                "data": {
                    "integration_id": integration_id,
                    "protocol_version": "v1",
                    "revision": entry["revision"],
                },
            }
        )
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            if payload.get("type") == "result":
                data = payload["data"]
                self.executor_results.append(data)
                waiter = self._result_waiters.pop(data["invocation_id"], None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(data)
        return ws

    async def _echo(self, request: web.Request) -> web.Response:
        """Harness-only signed POST, so tests can exercise bodies."""
        body = await request.read()
        return web.json_response({"success": True, "data": {"received": json.loads(body)}})

    async def _name(self, request: web.Request) -> web.Response:
        """Harness-only signed GET with a path segment that needs URL-decoding."""
        return web.json_response({"success": True, "data": {"name": request.match_info["name"]}})


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"success": False, "code": code, "message": code}, status=status)
