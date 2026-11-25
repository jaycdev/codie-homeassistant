"""HMAC-signed client for the Codie 1.6.0 local HTTP API."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    ClientWebSocketResponse,
    WSServerHandshakeError,
)
from yarl import URL

_LOGGER = logging.getLogger(__name__)

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

# 401 codes meaning the stored credentials no longer work, so Pairing has to run again.
REAUTH_CODES = frozenset(
    {"LOCAL_USER_MISMATCH", "LOCAL_SECRET_MISMATCH", "INVALID_SIGNATURE", "LOCAL_SESSION_EXPIRED"}
)
# The request's timestamp is too far from the Codie's clock; new credentials would not help.
CLOCK_SKEW_CODE = "REQUEST_EXPIRED"

# How far the Codie's `Date` header may be from our clock before a bad signature is put down to
# the clock rather than to the credentials.
CLOCK_TOLERANCE_S = 300

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


class CodieAuthError(CodieApiError):
    """The Codie no longer accepts our credentials; Pairing has to run again."""


class CodieNotConfirmedError(CodieApiError):
    """The Codie sent a screen command but could not confirm that the screen changed."""


def _clocks_differ(date_header: str | None) -> bool:
    """Whether the Codie's `Date` header puts its clock further from ours than the tolerance.

    The API reports no time of its own, so the header is all there is to check. That the real
    Codie sends it is unverified; without a usable header this says no, and the caller falls
    back to re-authentication.
    """
    if not date_header:
        return False
    try:
        codie_time = parsedate_to_datetime(date_header).timestamp()
    except TypeError, ValueError:
        return False
    return abs(codie_time - time.time()) > CLOCK_TOLERANCE_S


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


@dataclass(frozen=True)
class CodieBrightness:
    """The screen brightness: `level` is a percentage, None while the Codie cannot tell."""

    level: int | None
    writable: bool


@dataclass(frozen=True)
class CodieMusicPlayback:
    """The parts of the music playback state that the Music volume needs."""

    # None while the Codie is not ready to play music.
    volume: int | None
    # The last command the Codie took, whether the Codie ran it, and why it did not.
    request_id: str | None
    applied: bool
    error_code: str | None


@dataclass(frozen=True)
class CodieLightSwitches:
    """The side light switches. Each is None if the Codie left it out of the reply."""

    side_lights: bool | None
    music_side_lights: bool | None


def _as_dict(data: Any) -> dict[str, Any]:
    """Require the reply's `data` to be an object."""
    if not isinstance(data, dict):
        raise CodieResponseError("Reply has no data object")
    return data


def _number(data: dict[str, Any], key: str) -> int | None:
    """A whole number from `data`, or None if it is missing or not a number."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(value)


def _flag(data: dict[str, Any], key: str) -> bool | None:
    """A boolean from `data`, or None if it is missing or not a boolean."""
    value = data.get(key)
    return value if isinstance(value, bool) else None


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
        # Warn about clock skew once per outage, not on every retry.
        self._clock_skew_warned = False

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

    async def async_get_slot(self) -> int:
        """Read the number (1 to 5) of the active Slot."""
        data = _as_dict(await self.async_request("GET", "/data/user/slot"))
        slot = data.get("slot_number")
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise CodieResponseError("The Slot reply has no slot_number")
        return slot

    async def async_set_slot(self, slot: int) -> None:
        """Make `slot` the active Slot, switching to its Character.

        The Codie loads the Character's model afterwards; success does not mean it is shown yet.
        """
        await self.async_request("PUT", "/data/user/slot", {"slot_number": slot})

    async def async_get_characters(self) -> dict[int, str]:
        """Read the name of the Character in each Slot, by Slot number.

        A Character without a name maps to "". Entries without a Slot number are left out.
        """
        data = await self.async_request("GET", "/data/user/character/list")
        if not isinstance(data, list):
            raise CodieResponseError("The Character list reply has no list")
        characters: dict[int, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            slot = _number(item, "slot_number")
            if slot is None:
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                # A Character from a package may only have its name in the presentation.
                presentation = item.get("presentation")
                name = presentation.get("name") if isinstance(presentation, dict) else None
            characters[slot] = name.strip() if isinstance(name, str) else ""
        return characters

    async def async_get_voice_volume(self, slot: int) -> int | None:
        """Read the Voice volume (0 to 100) of the Character in `slot`.

        The slot is always passed: without it the Codie answers for Slot 1.
        """
        data = _as_dict(
            await self.async_request("GET", "/data/user/character", query={"slot": str(slot)})
        )
        return _number(data, "tts_volume")

    async def async_set_voice_volume(self, volume: int) -> None:
        """Set the Voice volume of the active Slot (no `slot_number` means the current one)."""
        await self.async_request("PUT", "/data/user/character", {"tts_volume": volume})

    async def async_get_music_playback(self) -> CodieMusicPlayback:
        """Read the Music volume (0 to 100) and the outcome of the last command."""
        data = _as_dict(await self.async_request("GET", "/data/user/music/playback"))
        volume_max = _number(data, "volume_max")
        volume = _number(data, "volume_level")
        if volume_max is not None and volume_max != 100:
            # The API says `volume_max` is 100 once the Codie is ready to play music.
            volume = None
        elif volume_max == 100 and volume is None:
            # The Codie leaves out fields that are zero.
            volume = 0
        request_id = data.get("request_id")
        error_code = data.get("error_code")
        return CodieMusicPlayback(
            volume=volume,
            request_id=request_id if isinstance(request_id, str) else None,
            applied=data.get("applied") is True,
            error_code=error_code if isinstance(error_code, str) and error_code else None,
        )

    async def async_set_music_volume(self, volume: int) -> str:
        """Ask the Codie to set the Music volume and return the command's `request_id`.

        The Codie only queues the command; `async_get_music_playback` shows whether it ran.
        """
        request_id = str(uuid.uuid4())
        await self.async_request(
            "POST",
            "/data/user/music/control",
            {"action": "SET_VOLUME", "volume_level": volume, "request_id": request_id},
        )
        return request_id

    async def async_get_brightness(self) -> CodieBrightness:
        """Read the screen brightness."""
        data = _as_dict(await self.async_request("GET", "/data/user/device/brightness"))
        return CodieBrightness(level=_number(data, "level"), writable=data.get("writable") is True)

    async def async_set_brightness(self, level: int) -> None:
        """Set the screen brightness (0 to 100)."""
        await self.async_request("PUT", "/data/user/device/brightness", {"level": level})

    async def async_get_screen(self) -> bool | None:
        """Read whether the screen is on. None if the Codie's two screens disagree."""
        data = _as_dict(await self.async_request("GET", "/data/user/device/screen"))
        return _flag(data, "on")

    async def async_set_screen(self, on: bool) -> None:
        """Turn the screen on or off.

        Raises `CodieNotConfirmedError` if the Codie sent the command but could not confirm it.
        """
        try:
            await self.async_request("PUT", "/data/user/device/screen", {"on": on})
        except CodieApiError as err:
            if err.status == HTTPStatus.CONFLICT or (
                err.status == HTTPStatus.SERVICE_UNAVAILABLE
                and err.code == "SCREEN_READBACK_FAILED"
            ):
                raise CodieNotConfirmedError(err.status, err.code, err.message) from err
            raise

    async def async_get_light_switches(self) -> CodieLightSwitches:
        """Read the Side lights and Music side lights switches."""
        data = _as_dict(await self.async_request("GET", "/data/user/light-master-switch"))
        return CodieLightSwitches(
            side_lights=_flag(data, "normalEnabled"), music_side_lights=_flag(data, "musicEnabled")
        )

    async def async_set_side_lights(self, enabled: bool) -> None:
        """Switch the Side lights, leaving the Music side lights as they are."""
        await self.async_request(
            "POST", "/data/user/light-master-switch", {"normalEnabled": enabled}
        )

    async def async_set_music_side_lights(self, enabled: bool) -> None:
        """Switch the Music side lights, leaving the Side lights as they are."""
        await self.async_request(
            "POST", "/data/user/light-master-switch", {"musicEnabled": enabled}
        )

    async def async_post_utterance(self, request_id: str, slot: int, text: str) -> None:
        """Make the Character in `slot` speak `text` now. Returns once the Codie accepted it."""
        await self.async_request(
            "POST",
            "/data/user/tts/utterances",
            {"request_id": request_id, "slot": slot, "text": text},
        )

    async def async_post_agent_result(self, text: str) -> None:
        """Queue `text` for the Character to speak once idle, and save it to chat history."""
        await self.async_request("POST", "/data/user/agent/results", {"text": text})

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
        except WSServerHandshakeError as err:
            if err.status == 401:
                # The handshake carries no error body; a signed request shows why it failed.
                await self.async_get_system()
            raise CodieConnectionError(f"The Codie refused the executor: {err.status}") from err
        except (ClientError, TimeoutError) as err:
            raise CodieConnectionError(f"Cannot reach the Codie at {self._host}") from err

    async def async_request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        query: dict[str, str] | None = None,
    ) -> Any:
        """Send a signed request. `path` is relative to the API prefix."""
        return await self._request(method, path, payload, signed=True, query=query)

    # --- plumbing ------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        signed: bool,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = URL.build(
            scheme="http",
            host=self._host,
            port=self._port,
            path=API_PREFIX + path,
            query=query or {},
        )
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
                codie_date = response.headers.get("Date")
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
            code = info.get("code")
            # Only the error's code and message: never the body as a whole, or the headers.
            _LOGGER.debug(
                "%s %s returned %s %s: %s", method, url.path, status, code, info.get("message")
            )
            if status == 401 and signed:
                if code == CLOCK_SKEW_CODE or (
                    code == "INVALID_SIGNATURE" and _clocks_differ(codie_date)
                ):
                    self._warn_clock_skew()
                elif code in REAUTH_CODES:
                    raise CodieAuthError(status, code, info.get("message"))
            raise CodieApiError(status, code, info.get("message"))
        if signed:
            self._clock_skew_warned = False
        if not raw.strip():
            return None
        if not isinstance(parsed, dict):
            raise CodieResponseError(f"{method} {path} did not return a JSON object")
        return parsed.get("data")

    def _warn_clock_skew(self) -> None:
        if not self._clock_skew_warned:
            self._clock_skew_warned = True
            _LOGGER.warning(
                "The Codie rejected a signed request. The clock of Home Assistant "
                "and the Codie differ too much; check the time on both"
            )

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
