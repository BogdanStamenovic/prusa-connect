from __future__ import annotations

import base64
import hashlib
import os
import stat
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from conftest import REFRESH, FakeConnect, make_jwt

from prusa_connect import auth, store
from prusa_connect.cli import main


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def test_pkce_challenge_matches_verifier() -> None:
    verifier, challenge = auth.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert 43 <= len(verifier) <= 128


def test_start_login_writes_private_pending_and_url(home: Path) -> None:
    url = auth.start_login(auth.CLIENTS["connect"])
    q = _query(url)
    assert url.startswith("https://account.prusa3d.com/o/authorize/?")
    assert q["client_id"] == "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4"
    assert q["redirect_uri"] == "https://connect.prusa3d.com/login/auth-callback"
    assert q["code_challenge_method"] == "S256"
    pending = store.read_json(store.pending_path())
    assert pending and pending["state"] == q["state"]
    assert "verifier" not in url and pending["verifier"] not in url
    assert stat.S_IMODE(os.stat(store.pending_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700


@pytest.mark.parametrize(
    "pasted,code,state",
    [
        ("https://connect.prusa3d.com/login/auth-callback?code=abc&state=st", "abc", "st"),
        ("prusaslicer://login?code=abc", "abc", None),
        ("code=abc&state=st", "abc", "st"),
        ("  abc  ", "abc", None),
        ("'https://connect.prusa3d.com/login/auth-callback?state=st&code=abc'", "abc", "st"),
    ],
)
def test_parse_pasted(pasted: str, code: str, state: str | None) -> None:
    assert auth.parse_pasted(pasted) == (code, state)


def test_parse_pasted_error() -> None:
    with pytest.raises(auth.AuthError, match="access_denied"):
        auth.parse_pasted("https://x/cb?error=access_denied")


def test_finish_login_exchanges_with_verifier(fake: FakeConnect) -> None:
    url = auth.start_login(auth.CLIENTS["connect"])
    state = _query(url)["state"]
    verifier = store.read_json(store.pending_path())["verifier"]  # type: ignore[index]
    auth.finish_login(f"https://connect.prusa3d.com/login/auth-callback?code=C0DE&state={state}", fake)
    form = dict(urllib.parse.parse_qsl(fake.requests[-1].body.decode()))  # type: ignore[union-attr]
    assert form == {
        "grant_type": "authorization_code",
        "code": "C0DE",
        "redirect_uri": "https://connect.prusa3d.com/login/auth-callback",
        "client_id": "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4",
        "code_verifier": verifier,
    }
    assert not store.pending_path().exists()
    assert stat.S_IMODE(os.stat(store.token_path()).st_mode) == 0o600
    assert store.read_json(store.token_path())["refresh_token"] == "refresh-SECRET-new"  # type: ignore[index]


def test_finish_login_rejects_foreign_state(fake: FakeConnect) -> None:
    auth.start_login(auth.CLIENTS["connect"])
    with pytest.raises(auth.AuthError, match="state mismatch"):
        auth.finish_login("https://x/cb?code=C&state=other", fake)
    assert fake.requests == []


def test_finish_login_expired_attempt(fake: FakeConnect) -> None:
    auth.start_login(auth.CLIENTS["connect"], now=time.time() - auth.PENDING_TTL_S - 5)
    with pytest.raises(auth.AuthError, match="too long ago"):
        auth.finish_login("C", fake)


def test_invalid_grant_maps_to_auth_error(fake: FakeConnect) -> None:
    auth.start_login(auth.CLIENTS["slicer"])
    fake.token_responses.append((400, {"error": "invalid_grant", "error_description": "Invalid code"}))
    with pytest.raises(auth.AuthError, match="invalid_grant"):
        auth.finish_login("C", fake)
    assert not store.token_path().exists()


def test_expired_access_token_is_refreshed(fake: FakeConnect, logged_in: dict[str, Any]) -> None:
    store.write_json(store.token_path(), {**logged_in, "expires_at": time.time() - 10})
    tok = auth.access_token(fake)
    form = dict(urllib.parse.parse_qsl(fake.requests[0].body.decode()))  # type: ignore[union-attr]
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == REFRESH
    stored = store.read_json(store.token_path())
    assert stored and stored["access_token"] == tok and stored["refresh_token"] == "refresh-SECRET-new"


def test_peer_refresh_is_reused(fake: FakeConnect, logged_in: dict[str, Any]) -> None:
    # Another process refreshed between our read and our lock: use its token.
    fresh = make_jwt(time.time() + 3600, "peer")
    store.write_json(store.token_path(), {**logged_in, "access_token": fresh, "expires_at": time.time() + 3600})
    assert auth.refresh(fake, stale_access="old") ["access_token"] == fresh
    assert fake.requests == []


def test_api_401_triggers_one_refresh_and_retry(fake: FakeConnect, logged_in: dict[str, Any]) -> None:
    fake.reject_tokens.add(logged_in["access_token"])
    assert main(["whoami"], transport=fake) == 0
    paths = [r.url.split(".com")[1].split("?")[0] for r in fake.requests]
    assert paths == ["/app/login", "/o/token/", "/app/login"]


def test_login_cli_split_flow(fake: FakeConnect, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["login", "--start"], transport=fake) == 0
    url = capsys.readouterr().out.strip()
    state = _query(url)["state"]
    assert main(["login", "--finish", f"https://connect.prusa3d.com/login/auth-callback?code=Q&state={state}"],
                transport=fake) == 0
    out, err = capsys.readouterr()
    assert out == "" and "SECRET" not in err
    assert main(["login", "--status", "--json"], transport=fake) == 0
    out = capsys.readouterr().out
    assert '"logged_in": true' in out and "SECRET" not in out


def test_login_status_when_logged_out(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["login", "--status"]) == 1


def test_logout(logged_in: dict[str, Any]) -> None:
    assert main(["logout"]) == 0
    assert not store.token_path().exists()


def test_not_logged_in_is_exit_1(fake: FakeConnect, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["printers"], transport=fake) == 1
    assert "prusa-connect login" in capsys.readouterr().err
