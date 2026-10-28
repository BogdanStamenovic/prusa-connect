"""Prusa Account OAuth2 (authorization code + PKCE) for a headless box.

Prusa does not publish a third-party OAuth client, so this reuses one of the
two public clients Prusa's own software uses:

* ``connect`` (default): the Connect web app's client, as published in
  https://connect.prusa3d.com/environment.js (``ACCOUNT_CLIENT_ID``). Its
  redirect is an ordinary https page, so after signing in on *any* device the
  code is sitting in the browser's address bar, ready to copy. Connect's
  callback page ignores a code whose ``state`` it did not issue, which is what
  makes this work; if Prusa changes that, the token exchange fails loudly.
* ``slicer``: PrusaSlicer's client (``m_account_client_id`` in PrusaSlicer's
  ServiceConfig.cpp, also present in /usr/bin/prusa-slicer 2.9.6). It
  redirects to ``prusaslicer://login?code=...``, which a browser either hands
  to PrusaSlicer or drops, so the code is only recoverable from developer
  tools. Kept as a fallback in case Prusa locks down the first one.

Both are public clients (no secret); PKCE S256 is what proves the code was
ours. Refresh tokens rotate on every use.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import store
from .transport import PrusaConnectError, Request, Transport

ACCOUNT_URL = "https://account.prusa3d.com"
TOKEN_URL = f"{ACCOUNT_URL}/o/token/"
AUTHORIZE_URL = f"{ACCOUNT_URL}/o/authorize/"

# How long before expiry we refresh proactively, and how long a started-but-
# unfinished login stays redeemable on our side (the server's own code
# lifetime is unknown and probably shorter; see README Limitations).
REFRESH_LEEWAY_S = 120
PENDING_TTL_S = 15 * 60


@dataclass(frozen=True)
class Client:
    name: str
    client_id: str
    redirect_uri: str
    scope: str


CLIENTS: dict[str, Client] = {
    "connect": Client(
        "connect",
        "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4",
        "https://connect.prusa3d.com/login/auth-callback",
        "basic_info",
    ),
    "slicer": Client(
        "slicer",
        "oamhmhZez7opFosnwzElIgE2oGgI2iJORSkw587O",
        "prusaslicer://login",
        "basic_info",
    ),
}


class AuthError(PrusaConnectError):
    """No usable login: the user has to run `prusa-connect login`."""


def client_by_id(client_id: str) -> Client:
    for c in CLIENTS.values():
        if c.client_id == client_id:
            return c
    raise AuthError("stored login uses an unknown client id; run `prusa-connect login` again")


# --- PKCE and the authorize URL ---------------------------------------------


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(client: Client, challenge: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "response_type": "code",
            "scope": client.scope,
            "redirect_uri": client.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def start_login(client: Client, now: float | None = None) -> str:
    """Create a pending login (verifier kept on disk, 0600) and return its URL."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    store.write_json(
        store.pending_path(),
        {
            "client": client.name,
            "verifier": verifier,
            "state": state,
            "created_at": now if now is not None else time.time(),
        },
    )
    return authorize_url(client, challenge, state)


def parse_pasted(text: str) -> tuple[str, str | None]:
    """Pull ``(code, state)`` out of whatever the user pasted.

    Accepts the full callback URL (https or prusaslicer://), a bare query
    string, or the bare code.
    """
    s = text.strip().strip("'\"<>")
    if not s:
        raise AuthError("nothing was pasted")
    if "=" not in s:
        return s, None
    query = urllib.parse.urlsplit(s).query if "://" in s else s.lstrip("?")
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        desc = params.get("error_description", [""])[0]
        raise AuthError(f"Prusa Account refused the sign-in: {params['error'][0]} {desc}".strip())
    code = params.get("code", [None])[0]
    if not code:
        raise AuthError("no code= in what was pasted; copy the whole address the browser ended on")
    return code, params.get("state", [None])[0]


def finish_login(pasted: str, transport: Transport, now: float | None = None) -> dict[str, Any]:
    now = now if now is not None else time.time()
    pending = store.read_json(store.pending_path())
    if not pending:
        raise AuthError("no login in progress; run `prusa-connect login` (or `login --start`) first")
    if now - float(pending.get("created_at", 0)) > PENDING_TTL_S:
        store.remove(store.pending_path())
        raise AuthError("the login started too long ago; run `prusa-connect login` again")
    code, state = parse_pasted(pasted)
    if state is not None and state != pending.get("state"):
        raise AuthError("the pasted URL belongs to a different login attempt (state mismatch)")
    client = CLIENTS[str(pending["client"])]
    tokens = _token_request(
        transport,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client.redirect_uri,
            "client_id": client.client_id,
            "code_verifier": str(pending["verifier"]),
        },
        now,
    )
    tokens["client_id"] = client.client_id
    store.write_json(store.token_path(), tokens)
    store.remove(store.pending_path())
    return tokens


# --- token exchange and refresh ---------------------------------------------


def _token_request(transport: Transport, form: dict[str, str], now: float) -> dict[str, Any]:
    resp = transport(
        Request(
            "POST",
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            urllib.parse.urlencode(form).encode(),
        )
    )
    try:
        data = json.loads(resp.body or b"{}")
    except json.JSONDecodeError:
        data = {}
    if resp.status != 200 or not isinstance(data, dict) or "access_token" not in data:
        # The error body from /o/token/ never contains a token, so it is safe to show.
        err = data.get("error", "") if isinstance(data, dict) else ""
        desc = data.get("error_description", "") if isinstance(data, dict) else ""
        detail = f"{err} {desc}".strip() or resp.body[:200].decode("utf-8", "replace")
        if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise AuthError(f"Prusa Account rejected the token request ({detail}); run `prusa-connect login`")
        raise PrusaConnectError(f"token request failed: HTTP {resp.status}: {detail}")
    expires_in = float(data.get("expires_in") or 0) or None
    exp = jwt_exp(str(data["access_token"]))
    data["expires_at"] = exp if exp is not None else (now + expires_in if expires_in else now + 300)
    data["obtained_at"] = now
    return data


def jwt_exp(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return float(payload["exp"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def load_tokens() -> dict[str, Any]:
    tokens = store.read_json(store.token_path())
    if not tokens or not tokens.get("access_token"):
        raise AuthError("not logged in; run `prusa-connect login`")
    return tokens


def refresh(transport: Transport, stale_access: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Refresh under the cross-process lock; return the fresh token set.

    If another process already refreshed while we waited for the lock (the
    stored access token is no longer the one we found stale), use theirs.
    """
    with store.locked():
        now = now if now is not None else time.time()
        tokens = load_tokens()
        refreshed_by_peer = stale_access is not None and tokens.get("access_token") != stale_access
        if refreshed_by_peer and float(tokens.get("expires_at", 0)) - REFRESH_LEEWAY_S > now:
            return tokens
        if not tokens.get("refresh_token"):
            raise AuthError("stored login has no refresh token; run `prusa-connect login`")
        client = client_by_id(str(tokens.get("client_id", CLIENTS["connect"].client_id)))
        new = _token_request(
            transport,
            {
                "grant_type": "refresh_token",
                "refresh_token": str(tokens["refresh_token"]),
                "client_id": client.client_id,
            },
            now,
        )
        new["client_id"] = client.client_id
        new.setdefault("refresh_token", tokens["refresh_token"])
        store.write_json(store.token_path(), new)
        return new


def access_token(transport: Transport, force_refresh: bool = False, now: float | None = None) -> str:
    now = now if now is not None else time.time()
    tokens = load_tokens()
    if force_refresh or float(tokens.get("expires_at", 0)) - REFRESH_LEEWAY_S <= now:
        tokens = refresh(transport, stale_access=str(tokens["access_token"]), now=now)
    return str(tokens["access_token"])


def login_info(now: float | None = None) -> dict[str, Any]:
    """What is safe to show about the stored login: never the tokens themselves."""
    now = now if now is not None else time.time()
    tokens = store.read_json(store.token_path())
    if not tokens:
        return {"logged_in": False, "token_file": str(store.token_path())}
    try:
        client = client_by_id(str(tokens.get("client_id", ""))).name
    except AuthError:
        client = "unknown"
    exp = float(tokens.get("expires_at", 0))
    return {
        "logged_in": bool(tokens.get("refresh_token") or exp > now),
        "client": client,
        "access_expires_in_s": int(exp - now),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "token_file": str(store.token_path()),
    }
