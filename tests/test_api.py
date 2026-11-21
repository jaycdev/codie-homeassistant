"""The signed API client, tested against the fake Codie."""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncGenerator

import pytest
from aiohttp import ClientSession

from custom_components.codie.api import (
    CodieApiError,
    CodieClient,
    CodieConnectionError,
    CodieCredentials,
    CodieResponseError,
)

from .fake_codie import API, FakeCodie, canonical_string, sign


@pytest.fixture
async def session(socket_enabled: None) -> AsyncGenerator[ClientSession]:
    async with ClientSession() as client_session:
        yield client_session


def make_client(
    session: ClientSession,
    codie: FakeCodie,
    *,
    secret: str | None = None,
    session_id: str | None = None,
) -> CodieClient:
    return CodieClient(
        session,
        codie.host,
        codie.port,
        CodieCredentials(
            local_secret=codie.local_secret if secret is None else secret,
            user_id=codie.user_id,
            local_session_id=codie.local_session_id if session_id is None else session_id,
        ),
    )


async def test_signed_request_is_accepted(session: ClientSession, fake_codie: FakeCodie) -> None:
    system = await make_client(session, fake_codie).async_get_system()

    assert system["machine_id"] == fake_codie.machine_id
    (request,) = fake_codie.signed_requests()
    assert request.rejected_with is None
    assert request.headers["X-Local-Payload-Mode"] == "empty"
    assert request.headers["X-Local-Body-Hash"] == hashlib.sha256(b"").hexdigest()
    assert request.headers["X-Local-User-Id"] == fake_codie.user_id


async def test_signature_matches_the_canonical_string(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    await make_client(session, fake_codie).async_request("POST", "/data/user/echo", {"a": 1})

    (request,) = fake_codie.signed_requests()
    h = request.headers
    canonical = canonical_string(
        "POST",
        f"{API}/data/user/echo",
        h["X-Local-Timestamp"],
        h["X-Local-Nonce"],
        "json",
        h["X-Local-Body-Hash"],
        fake_codie.user_id,
    )
    assert h["X-Local-Sig"] == sign(fake_codie.local_secret, canonical)


async def test_body_hash_covers_the_exact_bytes_sent(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    payload = {"text": "こんにちは — café", "n": [1, 2]}

    result = await make_client(session, fake_codie).async_request(
        "POST", "/data/user/echo", payload
    )

    assert result == {"received": payload}
    (request,) = fake_codie.signed_requests()
    assert request.rejected_with is None
    assert request.headers["X-Local-Payload-Mode"] == "json"
    assert request.headers["X-Local-Body-Hash"] == hashlib.sha256(request.body).hexdigest()
    # Compact separators and raw UTF-8, not \u escapes.
    assert request.body == '{"text":"こんにちは — café","n":[1,2]}'.encode()


async def test_every_request_uses_a_new_nonce(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    client = make_client(session, fake_codie)

    for _ in range(3):
        await client.async_get_system()

    nonces = [r.headers["X-Local-Nonce"] for r in fake_codie.signed_requests()]
    assert len(set(nonces)) == 3
    assert all(r.rejected_with is None for r in fake_codie.signed_requests())


async def test_timestamp_is_current_unix_seconds(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    before = int(time.time())
    await make_client(session, fake_codie).async_get_system()
    after = int(time.time())

    (request,) = fake_codie.signed_requests()
    assert before <= int(request.headers["X-Local-Timestamp"]) <= after


async def test_session_id_header_only_when_not_empty(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    await make_client(session, fake_codie).async_get_system()
    assert "X-Local-Session-Id" not in fake_codie.signed_requests()[0].headers

    fake_codie.local_session_id = "session-42"
    await make_client(session, fake_codie).async_get_system()
    second = fake_codie.signed_requests()[1]
    assert second.rejected_with is None
    assert second.headers["X-Local-Session-Id"] == "session-42"


async def test_public_requests_are_not_signed(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    client = CodieClient(session, fake_codie.host, fake_codie.port)

    await client.async_ping()
    bootstrap = await client.async_bootstrap()

    assert bootstrap.user_id == fake_codie.user_id
    assert bootstrap.device_logged_in is True
    assert all("X-Local-Sig" not in r.headers for r in fake_codie.requests)


# The fake must reject bad requests, or the tests above prove nothing.


async def raw_signed_get(
    session: ClientSession,
    codie: FakeCodie,
    *,
    secret: str | None = None,
    timestamp: int | None = None,
    nonce: str = "nonce-1",
) -> str | None:
    """Send a hand-built signed GET; returns the rejection code, if any."""
    path = f"{API}/data/user/system"
    ts = str(int(time.time()) if timestamp is None else timestamp)
    body_hash = hashlib.sha256(b"").hexdigest()
    canonical = canonical_string("GET", path, ts, nonce, "empty", body_hash, codie.user_id)
    headers = {
        "X-Local-Sig": sign(secret or codie.local_secret, canonical),
        "X-Local-Timestamp": ts,
        "X-Local-Nonce": nonce,
        "X-Local-Payload-Mode": "empty",
        "X-Local-Body-Hash": body_hash,
        "X-Local-User-Id": codie.user_id,
    }
    async with session.get(f"http://{codie.host}:{codie.port}{path}", headers=headers) as response:
        if response.status == 200:
            return None
        return str((await response.json())["code"])


async def test_fake_rejects_a_bad_signature(session: ClientSession, fake_codie: FakeCodie) -> None:
    assert await raw_signed_get(session, fake_codie) is None
    assert (
        await raw_signed_get(session, fake_codie, secret="wrong", nonce="n2") == "INVALID_SIGNATURE"
    )


async def test_fake_rejects_a_reused_nonce(session: ClientSession, fake_codie: FakeCodie) -> None:
    assert await raw_signed_get(session, fake_codie, nonce="same") is None
    assert await raw_signed_get(session, fake_codie, nonce="same") == "NONCE_REUSED"


async def test_fake_rejects_an_old_timestamp(session: ClientSession, fake_codie: FakeCodie) -> None:
    old = int(time.time()) - 301
    assert await raw_signed_get(session, fake_codie, timestamp=old) == "REQUEST_EXPIRED"


# And the client surfaces each rejection as an error carrying the Codie's code.


async def test_client_with_wrong_secret_gets_invalid_signature(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    client = make_client(session, fake_codie, secret="wrong-secret")

    with pytest.raises(CodieApiError) as error:
        await client.async_get_system()

    assert error.value.status == 401
    assert error.value.code == "INVALID_SIGNATURE"


async def test_client_with_a_skewed_clock_gets_request_expired(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    fake_codie.clock = lambda: time.time() + 3600

    with pytest.raises(CodieApiError) as error:
        await make_client(session, fake_codie).async_get_system()

    assert error.value.code == "REQUEST_EXPIRED"


async def test_unreachable_codie_raises_connection_error(session: ClientSession) -> None:
    client = CodieClient(session, "127.0.0.1", 1)

    with pytest.raises(CodieConnectionError):
        await client.async_ping()


async def test_path_is_signed_url_decoded(session: ClientSession, fake_codie: FakeCodie) -> None:
    result = await make_client(session, fake_codie).async_request("GET", "/data/user/name/a b é")

    assert result == {"name": "a b é"}
    (request,) = fake_codie.signed_requests()
    assert request.rejected_with is None
    assert "%20" in request.raw_path


async def test_non_json_reply_raises_a_response_error(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/auth/bootstrap"] = (200, b"<html>not json</html>")

    with pytest.raises(CodieResponseError):
        await CodieClient(session, fake_codie.host, fake_codie.port).async_bootstrap()


async def test_reply_missing_expected_fields_raises_a_response_error(
    session: ClientSession, fake_codie: FakeCodie
) -> None:
    fake_codie.overrides["/auth/pair/request"] = (200, b'{"success":true,"data":{}}')

    with pytest.raises(CodieResponseError):
        await CodieClient(session, fake_codie.host, fake_codie.port).async_pair_request()
