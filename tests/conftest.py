from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from prusa_connect import store
from prusa_connect.transport import Request, Response

ACCESS = "access-SECRET-aaaa"
REFRESH = "refresh-SECRET-bbbb"


def make_jwt(exp: float, tag: str = "x") -> str:
    def b64(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'tag': tag})}.sig-SECRET"


PRINTER = {
    "uuid": "aaaa-1111",
    "name": "Core One",
    "sn": "SN123",
    "team_id": 4242,
    "printer_model": "COREONE",
    "connect_state": "IDLE",
    "temp": {"temp_nozzle": 24.5, "target_nozzle": 0, "temp_bed": 23.0, "target_bed": 0},
}


class FakeConnect:
    """A stand-in for account.prusa3d.com + connect.prusa3d.com.

    Any request it does not recognise fails the test, so nothing can slip past
    to a real server.
    """

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.printer: dict[str, Any] = dict(PRINTER)
        self.reject_tokens: set[str] = set()
        self.token_responses: list[tuple[int, dict[str, Any]]] = []
        self.routes: dict[tuple[str, str], Callable[[Request], Response]] = {}

    def json(self, status: int, data: Any) -> Response:
        return Response(status, {"content-type": "application/json"}, json.dumps(data).encode())

    def __call__(self, req: Request) -> Response:
        self.requests.append(req)
        assert req.url.startswith(("https://account.prusa3d.com/", "https://connect.prusa3d.com/")), req.url
        path = req.url.split("?", 1)[0].split(".com", 1)[1]
        if path == "/o/token/":
            status, data = self.token_responses.pop(0) if self.token_responses else (
                200, {"access_token": make_jwt(time.time() + 3600, "new"), "refresh_token": "refresh-SECRET-new",
                      "expires_in": 3600, "token_type": "Bearer", "scope": "basic_info"})
            return self.json(status, data)
        token = req.headers.get("Authorization", "").removeprefix("Bearer ")
        if token in self.reject_tokens:
            return self.json(401, {"message": "expired"})
        key = (req.method, path)
        if key in self.routes:
            return self.routes[key](req)
        uuid = self.printer["uuid"]
        if key == ("GET", "/app/login"):
            return self.json(200, {"user": {"username": "kp", "email": "kp@example.com", "id": 7}})
        if key == ("GET", "/app/printers"):
            return self.json(200, {"printers": [self.printer]})
        if key == ("GET", f"/app/printers/{uuid}"):
            return self.json(200, self.printer)
        if key == ("GET", f"/app/printers/{uuid}/storages"):
            return self.json(200, {"storages": [{"type": "USB", "path": "/usb", "read_only": False}]})
        if key == ("GET", f"/app/printers/{uuid}/files"):
            return self.json(200, {"files": [{"type": "PRINT_FILE", "size": 10, "display_name": "a.bgcode",
                                              "path": "/usb/A~1.BGC"}]})
        if key == ("GET", f"/app/printers/{uuid}/jobs"):
            return self.json(200, {"jobs": [{"id": 9, "state": "FIN_OK", "start": 1.7e9, "time_printing": 3600,
                                             "file": {"display_name": "benchy.bgcode"}}]})
        if key == ("POST", "/app/users/teams/4242/uploads"):
            return self.json(200, {"id": 555, "hash": "HASH1", "state": "INITIATED"})
        if key == ("PUT", "/app/teams/4242/files/raw"):
            return self.json(200, {"hash": "HASH1", "name": "a.bgcode"})
        if key == ("POST", f"/app/printers/{uuid}/queue"):
            return self.json(200, {"id": 1})
        if key == ("POST", f"/app/printers/{uuid}/commands/sync"):
            return self.json(200, {"id": 77, "state": "FINISHED"})
        raise AssertionError(f"unexpected request {req.method} {req.url}")

    def writes(self) -> list[Request]:
        return [r for r in self.requests if r.method != "GET" and "/o/token/" not in r.url]


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    monkeypatch.setenv("PRUSA_CONNECT_HOME", str(d))
    monkeypatch.delenv("PRUSA_CONNECT_URL", raising=False)
    return d


@pytest.fixture
def fake() -> FakeConnect:
    return FakeConnect()


@pytest.fixture
def logged_in(home: Path) -> dict[str, Any]:
    tokens = {
        "access_token": make_jwt(time.time() + 3600, "cur"),
        "refresh_token": REFRESH,
        "expires_at": time.time() + 3600,
        "client_id": "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4",
    }
    store.write_json(store.token_path(), tokens)
    return tokens
