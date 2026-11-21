"""HMAC-signed client for the Codie 1.6.0 local HTTP API."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, ClientWebSocketResponse
from yarl import URL

API_PREFIX = "/v1/api"
WS_PREFIX = "/v1/ws"
REQUEST_TIMEOUT = ClientTimeout(total=10)
# The Codie pings every 20 s; sending our own at that rate also notices a silently dead link.
EXECUTOR_HEARTBEAT_S = 20

_WS_QUERY_HEADERS = {
    "sig": "X-Local-Sig",
    "timestamp": "X-Local-Timestamp",
    "nonce": "X-Local-Nonce",
    "payload_mode": "X-Local-Payload-Mode",
    "body_hash": "X-Local-Body-Hash",
    "user_id": "X-Local-User-Id",
    "session_id": "X-Local-Session-Id",
}

PAYLOAD_EMPTY = "empty"
PAYLOAD_JSON = "json"


class CodieError(Exception):
    """Base class for Codie client errors."""


class CodieConnectionError(CodieError):
    """The Codie could not be reached."""


class CodieResponseError(CodieError):
    """The Codie answered, but not with the shape the API documents."""


class CodieApiError(CodieError):
    """The Codie answered with an error status."""

    def __init__(self, status: int, code: str | None, message: str | None) -> None:
        super().__init__(f"HTTP {status} {code or ''}: {message or ''}".strip())
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CodieCredentials:
    """What pairing returns, plus the user it belongs to."""

    local_secret: str
    user_id: str
    local_session_id: str = ""


@dataclass(frozen=True)
class CodieBootstrap:
    """Pre-pairing information from `GET /auth/bootstrap`."""

    user_id: str
    device_logged_in: bool


@dataclass(frozen=True)
class CodieChallenge:
    """A pairing request the Codie is displaying a code for."""

    challenge_id: str
    expires_at_ms: int


@dataclass(frozen=True)
class CodiePairing:
    """The result of a successful `pair/confirm`."""

    local_secret: str
    local_session_id: str


def _as_dict(data: Any) -> dict[str, Any]:
    """Require the reply's `data` to be an object."""
    if not isinstance(data, dict):
        raise CodieResponseError("Reply has no data object")
    return data


class CodieClient:
    """Talks to one Codie. Signs every request except the five public ones."""

    def __init__(
        self,
        session: ClientSession,
        host: str,
        port: int,
        credentials: CodieCredentials | None = None,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._credentials = credentials

    # --- public endpoints ----------------------------------------------------------------

    async def async_ping(self) -> None:
        """Check that the Codie answers HTTP."""
        await self._request("GET", "/auth/ping", signed=False)

    async def async_bootstrap(self) -> CodieBootstrap:
        """Read the signed-in user and sign-in state, before pairing."""
        data = _as_dict(await self._request("GET", "/auth/bootstrap", signed=False))
        return CodieBootstrap(
            user_id=str(data.get("user_id") or ""),
            device_logged_in=bool(data.get("device_logged_in")),
        )

    async def async_pair_request(self) -> CodieChallenge:
        """Make the Codie display a pairing code."""
        data = _as_dict(await self._request("POST", "/auth/pair/request", signed=False))
        try:
            return CodieChallenge(
                challenge_id=str(data["challenge_id"]),
                expires_at_ms=int(data.get("expires_at_ms", 0)),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise CodieResponseError("pair/request reply is missing the challenge") from err

    async def async_pair_confirm(self, challenge_id: str, pairing_code: str) -> CodiePairing:
        """Exchange the displayed code for permanent credentials."""
        # Always permanent: session credentials expire after 12 h and on Codie restart.
        data = _as_dict(
            await self._request(
                "POST",
                "/auth/pair/confirm",
                {
                    "challenge_id": challenge_id,
                    "pairing_code": pairing_code,
                    "auth_scope": "permanent",
                },
                signed=False,
            )
        )
        secret = data.get("local_secret")
        if not isinstance(secret, str) or not secret:
            raise CodieResponseError("pair/confirm reply is missing the local secret")
        return CodiePairing(
            local_secret=secret,
            local_session_id=str(data.get("local_session_id") or ""),
        )

    async def async_pair_cancel(self, challenge_id: str) -> None:
        """Stop the Codie displaying the code for this challenge."""
        await self._request(
            "POST", "/auth/pair/cancel", {"challenge_id": challenge_id}, signed=False
        )

    # --- signed endpoints ----------------------------------------------------------------

    async def async_get_system(self) -> dict[str, Any]:
        """Read the device and user details (`machine_id` identifies the device)."""
        return _as_dict(await self.async_request("GET", "/data/user/system"))

    async def async_get_tool_integration(self, integration_id: str) -> dict[str, Any] | None:
        """Read a registered tool integration, or None if it does not exist."""
        try:
            return _as_dict(
                await self.async_request("GET", f"/data/user/tool-integrations/{integration_id}")
            )
        except CodieApiError as err:
            if err.status == 404:
                return None
            raise

    async def async_put_tool_integration(
        self, integration_id: str, catalog: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace the whole catalog. A stale revision raises `CodieApiError` with status 409."""
        return _as_dict(
            await self.async_request(
                "PUT", f"/data/user/tool-integrations/{integration_id}", catalog
            )
        )

    async def async_delete_tool_integration(self, integration_id: str) -> None:
        """Delete a tool integration. The reply has an empty body."""
        await self.async_request("DELETE", f"/data/user/tool-integrations/{integration_id}")

    async def async_connect_executor(self, integration_id: str) -> ClientWebSocketResponse:
        """Open the signed executor WebSocket for a tool integration."""
        path = f"{WS_PREFIX}/tool-integrations/{integration_id}/executor"
        headers = self._sign("GET", path, b"")
        # The signature covers the path without its query string.
        query = {
            key: headers[header] for key, header in _WS_QUERY_HEADERS.items() if header in headers
        }
        url = URL.build(scheme="ws", host=self._host, port=self._port, path=path, query=query)
        try:
            return await self._session.ws_connect(url, heartbeat=EXECUTOR_HEARTBEAT_S)
        except (ClientError, TimeoutError) as err:
            raise CodieConnectionError(f"Cannot reach the Codie at {self._host}") from err

    async def async_request(self, method: str, path: str, payload: Any | None = None) -> Any:
        """Send a signed request. `path` is relative to the API prefix."""
        return await self._request(method, path, payload, signed=True)

    # --- plumbing ------------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, payload: Any | None = None, *, signed: bool
    ) -> Any:
        url = URL.build(scheme="http", host=self._host, port=self._port, path=API_PREFIX + path)
        # The body is serialised once; the hash and the wire bytes are the same bytes.
        body = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        headers: dict[str, str] = {}
        if signed:
            headers.update(self._sign(method, url.path, body))
        if payload is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with self._session.request(
                method,
                url,
                data=body or None,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                raw = await response.read()
                status = response.status
        except (ClientError, TimeoutError) as err:
            raise CodieConnectionError(f"Cannot reach the Codie at {self._host}") from err

        parsed: Any = None
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None

        if status >= 400:
            info = parsed if isinstance(parsed, dict) else {}
            raise CodieApiError(status, info.get("code"), info.get("message"))
        if not raw.strip():
            return None
        if not isinstance(parsed, dict):
            raise CodieResponseError(f"{method} {path} did not return a JSON object")
        return parsed.get("data")

    def _sign(self, method: str, path: str, body: bytes) -> dict[str, str]:
        """Build the `X-Local-*` headers. A new nonce is used on every call."""
        if self._credentials is None:
            raise CodieError("Signed request without credentials")
        credentials = self._credentials
        timestamp = str(int(time.time()))
        nonce = str(uuid.uuid4())
        mode = PAYLOAD_JSON if body else PAYLOAD_EMPTY
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = "|".join(
            (method.upper(), path, timestamp, nonce, mode, body_hash, credentials.user_id)
        )
        # The key is the UTF-8 bytes of the secret string, not hex-decoded.
        signature = hmac.new(
            credentials.local_secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "X-Local-Sig": signature,
            "X-Local-Timestamp": timestamp,
            "X-Local-Nonce": nonce,
            "X-Local-Payload-Mode": mode,
            "X-Local-Body-Hash": body_hash,
            "X-Local-User-Id": credentials.user_id,
        }
        if credentials.local_session_id:
            headers["X-Local-Session-Id"] = credentials.local_session_id
        return headers
