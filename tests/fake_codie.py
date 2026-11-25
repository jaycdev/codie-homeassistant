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
from email.utils import formatdate
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
    invocation_ids: list[str] = field(default_factory=list)
    # Turn executor handshakes away, as when the Codie is restarting.
    refuse_executor: bool = False
    # Answer every signed request with this 401 code, whatever its signature.
    auth_error: str | None = None
    # What the `Date` header of every reply says: the clock plus `date_skew` seconds, so a test
    # can make the Codie's reported time differ without changing the timestamps it accepts.
    # `date_header` replaces it outright ("" stands for a reply without one, as aiohttp always
    # fills a missing header in).
    date_skew: float = 0.0
    date_header: str | None = None
    # The active Slot. `switch_slot_to` moves it right after the next read of it, the way a
    # user switching Characters between our read and our POST would.
    slot: int = 3
    switch_slot_to: int | None = None
    # The name of the Character in each Slot, as the Character list reports it.
    character_names: dict[int, str] = field(
        default_factory=lambda: {1: "Aiko", 2: "Bram", 3: "Cleo", 4: "Dax", 5: "Esme"}
    )
    # Refuse utterances with (status, code), as when the Codie is asleep or locked.
    utterance_error: tuple[int, str] | None = None
    utterances: list[dict[str, Any]] = field(default_factory=list)
    # Queued announcements, and the status to refuse them with (503: speech service not ready).
    agent_results: list[dict[str, Any]] = field(default_factory=list)
    agent_result_error: tuple[int, str] | None = None
    # Settings. Voice volume belongs to the Slot; the others are one value for the whole Codie.
    voice_volumes: dict[int, int] = field(
        default_factory=lambda: {1: 10, 2: 20, 3: 30, 4: 40, 5: 50}
    )
    music_volume: int = 55
    # The language the Character speaks, which `/data/user/system` reports.
    ai_language: str = "ja"
    # What `volume_max` says; the real Codie reports 100 once its music player is ready.
    music_volume_max: int | None = 100
    # A command is queued: it takes effect on this many playback reads after the POST. None:
    # never, as when the Codie is busy.
    music_apply_after_reads: int | None = 2
    # Whether the playback says `applied` once a command ran; if not, it only shows in the state.
    music_confirms: bool = True
    # Report this `error_code` for the command instead of applying it.
    music_error_code: str | None = None
    # Like the real Codie, a Music volume is rounded down to one of this many steps above 0.
    music_volume_steps: int = 11
    # What the music player is doing: IDLE, LOADING, PLAYING, PAUSED, COMPLETED or FAILED.
    music_status: str = "PAUSED"
    # The tracks on the card.
    music_tracks: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "track_id": "local:/music/one.mp3",
                "track_name": "Morning Light",
                "artist": "The Codies",
                "album": "Desk Songs",
                "duration_ms": 200_000,
            },
            {
                "track_id": "local:/music/two.mp3",
                "track_name": "Evening Hum",
                "artist": "The Codies",
                "album": "Desk Songs",
                "duration_ms": 180_000,
            },
        ]
    )
    # The queue NEXT and PREVIOUS move through, as indexes into `music_tracks`; a track can be
    # in it more than once. Entry i has the ID `entry-i`. Then the current entry (None: none).
    music_queue: list[int] = field(default_factory=lambda: [0, 1])
    music_entry: int | None = 0
    music_position_ms: int = 30_000
    # The Codie's own time, as a Unix time in milliseconds, for when the playback snapshot was
    # taken; 0 leaves it out.
    music_updated_at_ms: int = 0
    # Whether the music page is open, that is whether the Codie is in Music mode. Every
    # command except SET_VOLUME needs it.
    music_page_open: bool = True
    # The last command as playback reports it, and how many reads it has had.
    _music_command: dict[str, Any] | None = None
    _music_reads: int = 0
    brightness: int | None = 70
    brightness_writable: bool = True
    # None: the Codie's two screens disagree.
    screen_on: bool | None = True
    side_lights: bool = True
    music_side_lights: bool = False
    # Refuse every change to a setting with (status, code).
    setting_write_error: tuple[int, str] | None = None
    # Change the screen, then answer with (status, code): sent, but not confirmed.
    screen_unconfirmed: tuple[int, str] | None = None
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
        app.router.add_get(f"{API}/data/user/slot", self._slot)
        app.router.add_put(f"{API}/data/user/slot", self._slot_put)
        app.router.add_get(f"{API}/data/user/character/list", self._character_list)
        app.router.add_put(f"{API}/data/user/character", self._character_put)
        app.router.add_get(f"{API}/data/user/music/playback", self._playback)
        app.router.add_post(f"{API}/data/user/music/control", self._music_control)
        app.router.add_get(f"{API}/data/user/music/page", self._music_page)
        app.router.add_get(f"{API}/data/user/device/brightness", self._brightness_get)
        app.router.add_put(f"{API}/data/user/device/brightness", self._brightness_put)
        app.router.add_get(f"{API}/data/user/device/screen", self._screen_get)
        app.router.add_put(f"{API}/data/user/device/screen", self._screen_put)
        app.router.add_get(f"{API}/data/user/light-master-switch", self._lights_get)
        app.router.add_post(f"{API}/data/user/light-master-switch", self._lights_post)
        app.router.add_post(f"{API}/data/user/tts/utterances", self._utterance)
        app.router.add_post(f"{API}/data/user/agent/results", self._agent_result)
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
        self.invocation_ids.append(invocation_id)
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

    async def send_executor_error(self, code: str, **data: Any) -> None:
        """Send a protocol `error` message to the connected executor."""
        assert self._executor is not None
        await self._executor.send_json({"type": "error", "data": {"code": code, **data}})

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
        response = await self._respond(request, handler)
        # aiohttp would stamp the real clock; the fake's own clock is what a test can move.
        if not response.prepared:
            response.headers["Date"] = (
                self.date_header
                if self.date_header is not None
                else formatdate(self.clock() + self.date_skew, usegmt=True)
            )
        return response

    async def _respond(
        self, request: web.Request, handler: Callable[[web.Request], Any]
    ) -> web.StreamResponse:
        body = await request.read()
        recorded = RecordedRequest(
            request.method, request.path, dict(request.headers), body, request.raw_path
        )
        self.requests.append(recorded)
        if request.path not in PUBLIC_PATHS:
            error = self.auth_error or self._verify(request, body)
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
                    "ai_language": self.ai_language,
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
        if self.refuse_executor:
            return _error(503, "UNAVAILABLE")
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

    async def _slot(self, request: web.Request) -> web.Response:
        current = self.slot
        if self.switch_slot_to is not None:
            self.slot, self.switch_slot_to = self.switch_slot_to, None
        return web.json_response(
            {
                "success": True,
                "data": {"slot_number": current, "selected_cloth": 1, "version": f"{current}:1"},
            }
        )

    async def _slot_put(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.setting_write_error is not None:
            return _error(*self.setting_write_error)
        slot = body.get("slot_number")
        if set(body) != {"slot_number"} or not isinstance(slot, int) or not 1 <= slot <= 5:
            return _error(400, "INVALID_REQUEST")
        self.slot = slot
        return web.json_response({"success": True, "message": "ok"})

    async def _character_list(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "data": [
                    {"slot_number": slot, "name": name, "tts_volume": self.voice_volumes[slot]}
                    for slot, name in sorted(self.character_names.items())
                ],
            }
        )

    async def _character_put(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.setting_write_error is not None:
            return _error(*self.setting_write_error)
        # Without `slot_number` (or with 0) the change goes to the Slot that is active now.
        slot = body.get("slot_number") or self.slot
        volume = body.get("tts_volume")
        if set(body) - {"slot_number", "tts_volume"} or not isinstance(volume, int):
            return _error(400, "INVALID_REQUEST")
        self.voice_volumes[slot] = volume
        return web.json_response(
            {"success": True, "data": {"slot_number": slot, "tts_volume": volume}}
        )

    async def _playback(self, request: web.Request) -> web.Response:
        command = self._music_command
        if command is not None:
            self._music_reads += 1
            if self._music_reads >= (self.music_apply_after_reads or 1 << 30) or (
                self.music_error_code is not None
            ):
                if self.music_error_code is not None:
                    command["error_code"] = self.music_error_code
                elif not command["ran"]:
                    self._apply_music_command(command["body"])
                    command["ran"] = True
                    command["applied"] = self.music_confirms
        data: dict[str, Any] = {}
        if self.music_volume_max is not None:
            data["volume_max"] = self.music_volume_max
        # Like the real Codie, leave out a field that is zero or empty.
        if self.music_volume:
            data["volume_level"] = self.music_volume
        if self.music_status not in ("", "IDLE"):
            data["status"] = self.music_status
            if self.music_status == "PLAYING":
                data["is_playing"] = True
        if self.music_queue:
            data["queue"] = {
                "entries": [
                    {"id": f"entry-{i}", "track_id": self.music_tracks[track]["track_id"]}
                    for i, track in enumerate(self.music_queue)
                ]
            }
        if self.music_entry is not None:
            data["queue"]["current_entry_id"] = f"entry-{self.music_entry}"
            track = self.music_tracks[self.music_queue[self.music_entry]]
            data.update({k: v for k, v in track.items() if v})
            if self.music_position_ms:
                data["position_ms"] = self.music_position_ms
        if self.music_updated_at_ms:
            data["updated_at_ms"] = self.music_updated_at_ms
        if command is not None:
            data["request_id"] = command["request_id"]
            data["applied"] = command["applied"]
            if "error_code" in command:
                data["error_code"] = command["error_code"]
        return web.json_response({"success": True, "data": data})

    def _music_level(self, volume: int) -> int:
        """The Music volume the real Codie reports after it is asked for `volume`."""
        steps = self.music_volume_steps
        step = volume * steps // 100
        if volume and not step:
            # A level just above 0 still gets the first step.
            step = 1
        return step * 100 // steps

    def _apply_music_command(self, body: dict[str, Any]) -> None:
        """Run a queued command, the way the real Codie does once it gets to it."""
        action = body["action"]
        if action == "SET_VOLUME":
            self.music_volume = self._music_level(body["volume_level"])
        elif action == "PLAY":
            if self.music_entry is None:
                self.music_entry = 0
            self.music_status = "PLAYING"
        elif action == "PAUSE":
            self.music_status = "PAUSED"
        elif action == "STOP":
            # STOP clears the current track and keeps the queue.
            self.music_status = "IDLE"
            self.music_entry = None
            self.music_position_ms = 0
        elif action in ("NEXT", "PREVIOUS"):
            step = 1 if action == "NEXT" else -1
            self.music_entry = ((self.music_entry or 0) + step) % len(self.music_queue)
            self.music_position_ms = 0

    async def _music_control(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.setting_write_error is not None:
            return _error(*self.setting_write_error)
        try:
            uuid.UUID(body["request_id"])
        except KeyError, ValueError:
            return _error(400, "INVALID_REQUEST")
        action = body.get("action")
        if action == "SET_VOLUME":
            if set(body) != {"action", "request_id", "volume_level"} or not isinstance(
                body["volume_level"], int
            ):
                return _error(400, "INVALID_REQUEST")
        elif action in ("PLAY", "PAUSE", "STOP", "NEXT", "PREVIOUS"):
            # Without a target, PLAY resumes the current entry.
            if set(body) != {"action", "request_id"}:
                return _error(400, "INVALID_REQUEST")
            if not self.music_page_open:
                return _error(409, "MODE_INACTIVE")
        else:
            return _error(400, "INVALID_REQUEST")
        self._music_reads = 0
        self._music_command = {
            "request_id": body["request_id"],
            "body": body,
            "ran": False,
            "applied": False,
        }
        return web.json_response({"success": True, "data": {"request_id": body["request_id"]}})

    async def _music_page(self, request: web.Request) -> web.Response:
        # Like the real Codie, leave out a field that is false.
        data = {"is_open": True} if self.music_page_open else {}
        return web.json_response({"success": True, "data": data})

    async def _brightness_get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "data": {"level": self.brightness, "writable": self.brightness_writable},
            }
        )

    async def _brightness_put(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.setting_write_error is not None:
            return _error(*self.setting_write_error)
        if set(body) != {"level"} or not isinstance(body["level"], int):
            return _error(400, "INVALID_REQUEST")
        self.brightness = body["level"]
        return await self._brightness_get(request)

    async def _screen_get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"success": True, "data": {"on": self.screen_on, "channels": [], "command_sent": False}}
        )

    async def _screen_put(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.setting_write_error is not None:
            return _error(*self.setting_write_error)
        if set(body) != {"on"} or not isinstance(body["on"], bool):
            return _error(400, "INVALID_REQUEST")
        self.screen_on = body["on"]
        if self.screen_unconfirmed is not None:
            return _error(*self.screen_unconfirmed)
        return await self._screen_get(request)

    async def _lights_get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "data": {
                    # `enabled` is an alias of `normalEnabled`, not of both switches.
                    "enabled": self.side_lights,
                    "normalEnabled": self.side_lights,
                    "musicEnabled": self.music_side_lights,
                },
            }
        )

    async def _lights_post(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.setting_write_error is not None:
            return _error(*self.setting_write_error)
        # `enabled` aliases `normalEnabled`; the integration must never send it.
        if not body or set(body) - {"normalEnabled", "musicEnabled"}:
            return _error(400, "INVALID_REQUEST")
        self.side_lights = body.get("normalEnabled", self.side_lights)
        self.music_side_lights = body.get("musicEnabled", self.music_side_lights)
        return await self._lights_get(request)

    async def _utterance(self, request: web.Request) -> web.Response:
        body = await request.json()
        if set(body) != {"request_id", "slot", "text"}:
            return _error(400, "INVALID_REQUEST")
        try:
            uuid.UUID(body["request_id"])
        except ValueError:
            return _error(400, "INVALID_REQUEST")
        if not 1 <= len(str(body["text"]).strip()) <= 16000:
            return _error(400, "INVALID_REQUEST")
        if self.utterance_error is not None:
            return _error(*self.utterance_error)
        if body["slot"] != self.slot:
            return _error(409, "SLOT_MISMATCH")
        self.utterances.append(body)
        return web.json_response(
            {"success": True, "data": {"utterance_id": str(uuid.uuid4()), "state": "queued"}},
            status=202,
        )

    async def _agent_result(self, request: web.Request) -> web.Response:
        body = await request.json()
        if set(body) != {"text"} or not isinstance(body["text"], str):
            return _error(400, "INVALID_REQUEST")
        if not 1 <= len(body["text"].strip()) <= 200:
            return _error(400, "INVALID_REQUEST")
        if self.agent_result_error is not None:
            return _error(*self.agent_result_error)
        self.agent_results.append(body)
        return web.json_response({"success": True}, status=202)

    async def _echo(self, request: web.Request) -> web.Response:
        """Harness-only signed POST, so tests can exercise bodies."""
        body = await request.read()
        return web.json_response({"success": True, "data": {"received": json.loads(body)}})

    async def _name(self, request: web.Request) -> web.Response:
        """Harness-only signed GET with a path segment that needs URL-decoding."""
        return web.json_response({"success": True, "data": {"name": request.match_info["name"]}})


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"success": False, "code": code, "message": code}, status=status)
