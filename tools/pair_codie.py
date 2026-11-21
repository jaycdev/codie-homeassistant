#!/usr/bin/env python3
"""Pair with a CODE27 Codie and probe the facts listed in the codie-integration spec.

Usage:
    python tools/pair_codie.py http://DEVICE_IP:8081
    python tools/pair_codie.py http://DEVICE_IP:8081 --probe

Pairing always requests permanent credentials (session credentials expire after 12 h
and on Codie restart). The credentials file is created exclusively and is never
overwritten; if it exists it is reused. By default it lives in ~/.codie-ha/, outside
this repository. Never commit it or paste its contents anywhere.

--probe makes signed read-only calls and prints what .scratch/codie-integration
issue 01 needs: GET /data/user/system (identifiers and capabilities),
GET /data/user/tool-integrations and GET /data/user/slot. Secrets are never printed.

Only the Python standard library is used. Pass the device URL without /v1/api.
Based on examples/local_api.py from https://github.com/jaycdev/codie-http-api (1.6.0).
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

API = "/v1/api"
DEFAULT_CREDENTIALS = Path.home() / ".codie-ha" / "credentials.json"
SECRET_KEYS = ("local_secret", "local_session_id")


class APIError(Exception):
    def __init__(self, status, payload):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


class CodieClient:
    def __init__(self, base_url, credentials=None):
        self.base_url = base_url.rstrip("/")
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.netloc or url.path or url.query or url.fragment:
            raise ValueError("Use a device URL such as http://DEVICE_IP:8081, without an API path or query parameters.")
        self.credentials = credentials or {}

    def _headers(self, method, path, body, mode):
        timestamp = str(int(time.time()))
        nonce = str(uuid.uuid4())
        user_id = self.credentials.get("user_id", "")
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = "|".join((method.upper(), unquote(urlsplit(path).path), timestamp, nonce, mode, body_hash, user_id))
        # The key is the UTF-8 bytes of the original local_secret string, not hex-decoded.
        signature = hmac.new(self.credentials["local_secret"].encode(), canonical.encode(), hashlib.sha256).hexdigest()
        headers = {"X-Local-Sig": signature, "X-Local-Timestamp": timestamp, "X-Local-Nonce": nonce,
                   "X-Local-Payload-Mode": mode, "X-Local-Body-Hash": body_hash, "X-Local-User-Id": user_id}
        if self.credentials.get("local_session_id"):
            headers["X-Local-Session-Id"] = self.credentials["local_session_id"]
        return headers

    def request(self, method, path, payload=None, *, public=False, timeout=30):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        mode = "json" if payload is not None else "empty"
        headers = {} if public else self._headers(method, path, body, mode)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=body if method.upper() != "GET" else None,
                          headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw.strip() else {}
        except HTTPError as error:
            raw = error.read()
            try:
                detail = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                detail = raw.decode(errors="replace")
            raise APIError(error.code, detail) from error
        except URLError as error:
            raise RuntimeError(f"Cannot reach {self.base_url}: {error.reason}") from error


def pair(client, read_code):
    """Run the pairing flow; always cancels the challenge if it does not complete."""
    client.request("GET", API + "/auth/ping", public=True)
    bootstrap = client.request("GET", API + "/auth/bootstrap", public=True)["data"]
    if not bootstrap.get("device_logged_in"):
        raise RuntimeError("No user is signed in on the Codie. Sign in on the device and try again.")
    challenge = client.request("POST", API + "/auth/pair/request", public=True)["data"]
    challenge_id = challenge["challenge_id"]
    confirmed = False
    try:
        remaining = max(0, int(challenge.get("expires_at_ms", 0) / 1000 - time.time()))
        print(f"The Codie is showing a pairing code (valid for about {remaining} s).")
        code = read_code()
        result = client.request("POST", API + "/auth/pair/confirm", {
            "challenge_id": challenge_id, "pairing_code": code, "auth_scope": "permanent"}, public=True)["data"]
        confirmed = True
    finally:
        if not confirmed:
            try:
                client.request("POST", API + "/auth/pair/cancel", {"challenge_id": challenge_id}, public=True)
                print("Pairing request cancelled on the Codie.", file=sys.stderr)
            except (APIError, RuntimeError):
                pass
    keys = ("local_secret", "local_session_id", "local_session_expires_at_ms", "auth_scope")
    credentials = {key: result[key] for key in keys if key in result}
    credentials["user_id"] = bootstrap.get("user_id", "")
    return credentials


def save_credentials(path, credentials):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create, owner-only; never overwrite an existing file.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(credentials, output, ensure_ascii=False, indent=2)
        output.write("\n")


def probe(client):
    """Signed read-only calls that answer .scratch/codie-integration issue 01."""
    system = client.request("GET", API + "/data/user/system")["data"]
    print("\n== GET /data/user/system ==")
    print("(candidates for the config entry unique ID: machine_id, user_id)")
    print(json.dumps(system, indent=2, ensure_ascii=False))
    try:
        slot = client.request("GET", API + "/data/user/slot")["data"]
        print("\n== GET /data/user/slot ==")
        print(json.dumps(slot, indent=2, ensure_ascii=False))
    except APIError as error:
        print(f"\nGET /data/user/slot failed: {error}", file=sys.stderr)
    integrations = client.request("GET", API + "/data/user/tool-integrations")["data"]
    print("\n== GET /data/user/tool-integrations ==")
    print("(re-run after switching Slots to see whether registrations are per user or per Slot)")
    print(json.dumps(integrations, indent=2, ensure_ascii=False))
    print("\nNo mDNS lookup is done here. Check separately, for example: dns-sd -B _services._dns-sd._udp local.")


def main():
    parser = argparse.ArgumentParser(description="Pair with a CODE27 Codie and probe its API.")
    parser.add_argument("device", help="http://DEVICE_IP:8081 (no /v1/api)")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS,
                        help="credentials file to create or reuse (default: %(default)s)")
    parser.add_argument("--probe", action="store_true", help="after pairing, print system info, Slot and tool integrations")
    args = parser.parse_args()

    client = CodieClient(args.device)
    if args.credentials.exists():
        client.credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
        missing = [key for key in ("local_secret", "user_id") if key not in client.credentials]
        if missing:
            raise ValueError(f"{args.credentials} is missing {', '.join(missing)}; pair again with a new filename.")
        print(f"Using existing credentials: {args.credentials}")
    else:
        client.credentials = pair(client, lambda: input("Enter the pairing code shown on the Codie: ").strip())
        save_credentials(args.credentials, client.credentials)
        print(f"Pairing succeeded. Credentials saved to {args.credentials}")
        print(f"auth_scope={client.credentials.get('auth_scope')}, "
              f"local_session_id {'set' if client.credentials.get('local_session_id') else 'empty'}")
    if args.probe:
        probe(client)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, APIError, EOFError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
