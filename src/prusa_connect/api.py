"""Prusa Connect's ``/app`` REST API, read side and guarded write side.

Prusa does not document this API. Paths and payloads come from what
PrusaSlicer sends (src/slic3r-shared/.../PrintHostPrusaConnect.cpp: the
upload pair ``POST /app/users/teams/{team}/uploads`` then
``PUT /app/teams/{team}/files/raw?upload_id=``) and from what the Connect web
app sends (``/app/printers/{uuid}/commands/sync`` with
``{"command": ..., "kwargs": ...}``). Treat them as observed, not promised.

Write operations are built as a `Plan` first. The CLI prints the plan for
``--dry-run`` and executes the very same object otherwise, so a dry run shows
exactly what would go over the wire.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import auth
from .transport import PrusaConnectError, Request, Response, Transport

CONNECT_URL = "https://connect.prusa3d.com"

# States in which each printer-moving command makes sense. Connect enforces its
# own rules too; checking first gives a clear message instead of an HTTP 409.
COMMAND_STATES: dict[str, tuple[str, ...]] = {
    "PAUSE_PRINT": ("PRINTING",),
    "RESUME_PRINT": ("PAUSED",),
    "STOP_PRINT": ("PRINTING", "PAUSED", "ATTENTION"),
}

# Starting a print is allowed only from READY: in Connect that state means a
# human pressed "set ready" (or confirmed on the printer), i.e. said the bed is
# clear. IDLE/FINISHED/STOPPED may still have the last part on the plate.
PRINT_STATES = ("READY",)

ALLOWED_UPLOAD_SUFFIXES = (".gcode", ".bgcode")


class APIError(PrusaConnectError):
    def __init__(self, method: str, path: str, status: int, body: bytes) -> None:
        self.status = status
        detail = body[:300].decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = str(parsed.get("message") or parsed.get("detail") or parsed.get("code") or detail)
        except (json.JSONDecodeError, ValueError):
            pass
        super().__init__(f"{method} {path}: HTTP {status}: {detail}")


class RefusedError(PrusaConnectError):
    """A guard said no (wrong printer state, bad file). Nothing was sent."""


class Connect:
    def __init__(self, transport: Transport, base_url: str = CONNECT_URL) -> None:
        self.transport = transport
        self.base_url = base_url.rstrip("/")

    def url(self, path: str, query: dict[str, Any] | None = None) -> str:
        u = self.base_url + path
        if query:
            u += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        return u

    def _send(self, method: str, url: str, headers: dict[str, str], body: Any, timeout: float) -> Response:
        def once(token: str) -> Response:
            payload = body()
            try:
                return self.transport(
                    Request(method, url, {**headers, "Authorization": f"Bearer {token}"}, payload, timeout)
                )
            finally:
                if hasattr(payload, "close"):
                    payload.close()

        resp = once(auth.access_token(self.transport))
        if resp.status == 401:
            resp = once(auth.access_token(self.transport, force_refresh=True))
            if resp.status == 401:
                raise auth.AuthError("Prusa Connect rejected the login even after a refresh; run `prusa-connect login`")
        return resp

    def call(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        json_body: Any = None,
        raw_body: Callable[[], Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        hdrs = {"Accept": "application/json", **(headers or {})}
        if json_body is not None:
            hdrs["Content-Type"] = "application/json"
            payload = json.dumps(json_body).encode()

            def body() -> Any:
                return payload
        elif raw_body is not None:
            body = raw_body
        else:
            def body() -> Any:
                return None

        resp = self._send(method, self.url(path, query), hdrs, body, timeout)
        if resp.status >= 400:
            raise APIError(method, path, resp.status, resp.body)
        if not resp.body.strip():
            return None
        try:
            return json.loads(resp.body)
        except json.JSONDecodeError:
            return resp.body.decode("utf-8", "replace")

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        return self.call("GET", path, query)

    # --- read side ----------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        data = self.get("/app/login")
        if not isinstance(data, dict):
            raise PrusaConnectError("GET /app/login returned something that is not an object")
        return data

    def printers(self) -> list[dict[str, Any]]:
        data = self.get("/app/printers", {"limit": 100})
        items = data.get("printers") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PrusaConnectError("GET /app/printers no longer has a 'printers' list; the API changed")
        return [p for p in items if isinstance(p, dict)]

    def resolve(self, ref: str | None) -> dict[str, Any]:
        printers = self.printers()
        if not printers:
            raise PrusaConnectError("this Prusa Connect account has no printers")
        listing = ", ".join(f"{p.get('name')!r} ({p.get('uuid')})" for p in printers)
        if not ref:
            if len(printers) == 1:
                return printers[0]
            raise PrusaConnectError(f"the account has {len(printers)} printers; name one of: {listing}")
        r = ref.strip().lower()
        for p in printers:
            if r in (str(p.get("uuid", "")).lower(), str(p.get("name", "")).lower(), str(p.get("sn", "")).lower()):
                return p
        partial = [p for p in printers if r in str(p.get("name", "")).lower() or str(p.get("uuid", "")).lower().startswith(r)]
        if len(partial) == 1:
            return partial[0]
        raise PrusaConnectError(f"no single printer matches {ref!r}; printers: {listing}")

    def printer(self, uuid: str) -> dict[str, Any]:
        data = self.get(f"/app/printers/{_q(uuid)}")
        if not isinstance(data, dict):
            raise PrusaConnectError("printer record is not an object")
        return data

    def storages(self, uuid: str) -> list[dict[str, Any]]:
        data = self.get(f"/app/printers/{_q(uuid)}/storages")
        items = data.get("storages") if isinstance(data, dict) else data
        return [s for s in items if isinstance(s, dict)] if isinstance(items, list) else []

    def files(self, uuid: str, path: str | None, limit: int, offset: int) -> Any:
        return self.get(f"/app/printers/{_q(uuid)}/files", {"path": path, "limit": limit, "offset": offset})

    def cloud_files(self, team_id: Any, limit: int, offset: int) -> Any:
        return self.get(f"/app/teams/{_q(str(team_id))}/files", {"limit": limit, "offset": offset})

    def jobs(self, uuid: str, limit: int, offset: int, states: list[str]) -> Any:
        q: dict[str, Any] = {"limit": limit, "offset": offset}
        path = f"/app/printers/{_q(uuid)}/jobs"
        if states:
            # Repeated ?state=A&state=B, which urlencode needs a list of pairs for.
            path += "?" + urllib.parse.urlencode([("state", s.upper()) for s in states])
            return self.call("GET", path + "&" + urllib.parse.urlencode(q))
        return self.get(path, q)

    def queue(self, uuid: str) -> Any:
        return self.get(f"/app/printers/{_q(uuid)}/queue")

    def supported_commands(self, uuid: str) -> Any:
        return self.get(f"/app/printers/{_q(uuid)}/supported-commands")


def _q(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def printer_state(p: dict[str, Any]) -> str:
    return str(p.get("connect_state") or p.get("printer_state") or p.get("state") or "UNKNOWN").upper()


def summarize(p: dict[str, Any]) -> dict[str, Any]:
    temp: dict[str, Any] = p["temp"] if isinstance(p.get("temp"), dict) else {}
    job: dict[str, Any] = p["job_info"] if isinstance(p.get("job_info"), dict) else {}
    out: dict[str, Any] = {
        "name": p.get("name"),
        "uuid": p.get("uuid"),
        "model": p.get("printer_model") or p.get("printer_type_name"),
        "state": printer_state(p),
        "nozzle": _pair(temp.get("temp_nozzle"), temp.get("target_nozzle")),
        "bed": _pair(temp.get("temp_bed"), temp.get("target_bed")),
        "team_id": p.get("team_id"),
    }
    if job:
        out["job"] = {
            "id": job.get("id"),
            "file": job.get("display_name") or str(job.get("path", "")).lstrip("/") or None,
            "progress": job.get("progress"),
            "time_printing_s": job.get("time_printing"),
            "time_remaining_s": job.get("time_remaining"),
        }
    return out


def _pair(actual: Any, target: Any) -> dict[str, Any] | None:
    if actual is None and target is None:
        return None
    return {"actual": actual, "target": target}


# --- write side: plans --------------------------------------------------------


@dataclass
class Step:
    method: str
    path: str
    query: dict[str, Any] = field(default_factory=dict)
    json_body: Any = None
    upload_file: Path | None = None
    note: str = ""

    def describe(self, connect: Connect) -> dict[str, Any]:
        d: dict[str, Any] = {
            "method": self.method,
            "url": connect.url(self.path, self.query or None),
            "headers": {"Authorization": "Bearer <stored token, not shown>"},
        }
        if self.json_body is not None:
            d["headers"]["Content-Type"] = "application/json"
            d["json"] = self.json_body
        if self.upload_file is not None:
            d["headers"]["Content-Type"] = "text/x.gcode"
            d["body"] = f"<contents of {self.upload_file} ({self.upload_file.stat().st_size} bytes)>"
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class Plan:
    action: str
    printer: dict[str, Any]
    steps: list[Step]
    moves_printer: bool
    refusal: str | None = None

    def describe(self, connect: Connect) -> dict[str, Any]:
        return {
            "action": self.action,
            "printer": {"name": self.printer.get("name"), "uuid": self.printer.get("uuid"),
                        "state": printer_state(self.printer)},
            "moves_printer": self.moves_printer,
            "would_refuse": self.refusal,
            "requests": [s.describe(connect) for s in self.steps],
        }


def _check_file(local: Path) -> None:
    if not local.is_file():
        raise RefusedError(f"{local} is not a file")
    if local.suffix.lower() not in ALLOWED_UPLOAD_SUFFIXES:
        raise RefusedError(f"{local.name}: only sliced {'/'.join(ALLOWED_UPLOAD_SUFFIXES)} files can be sent")


def _default_destination(connect: Connect, uuid: str) -> str:
    storages = connect.storages(uuid)
    writable = [s for s in storages if not s.get("read_only") and (s.get("path") or s.get("mountpoint"))]
    writable.sort(key=lambda s: 0 if str(s.get("type", "")).upper() == "USB" else 1)
    if not writable:
        raise RefusedError("the printer reports no writable storage (is a USB drive inserted?)")
    return "/" + str(writable[0].get("path") or writable[0].get("mountpoint")).strip("/")


def plan_upload(connect: Connect, printer: dict[str, Any], local: Path, destination: str | None,
                filename: str | None) -> Plan:
    _check_file(local)
    team = printer.get("team_id")
    if team is None:
        raise PrusaConnectError("printer record has no team_id; cannot address the upload")
    dest = destination or _default_destination(connect, str(printer["uuid"]))
    name = filename or local.name
    steps = [
        Step("POST", f"/app/users/teams/{_q(str(team))}/uploads",
             json_body={"destination": dest, "filename": name, "size": local.stat().st_size,
                        "printer_uuid": printer["uuid"]},
             note="registers the upload; returns its id"),
        Step("PUT", f"/app/teams/{_q(str(team))}/files/raw", query={"upload_id": "<id from previous step>"},
             upload_file=local,
             note="stores the file in Connect; Connect then copies it to the printer in the background"),
    ]
    return Plan("upload", printer, steps, moves_printer=False)


def plan_print(connect: Connect, printer: dict[str, Any], target: str, destination: str | None) -> Plan:
    """Start a print of a local file (upload, then queue first) or of a file already on the printer."""
    state = printer_state(printer)
    refusal = None
    if state not in PRINT_STATES:
        refusal = (
            f"printer {printer.get('name')!r} is {state}, not READY. A print starts only after a human "
            "has confirmed the bed is clear by setting the printer READY (on the printer, or "
            "'Set ready' in Prusa Connect). Nothing was sent."
        )
    local = Path(target).expanduser()
    if local.exists():
        up = plan_upload(connect, printer, local, destination, None)
        steps = up.steps + [
            Step("POST", f"/app/printers/{_q(str(printer['uuid']))}/queue",
                 json_body={"hash": "<hash from upload>", "team_id": printer.get("team_id"), "position": 0},
                 note="puts the file first in the queue; a READY printer starts it once the file has arrived. "
                      "set_ready is never sent: READY must come from a human"),
        ]
    else:
        if not target.startswith("/"):
            raise RefusedError(f"{target!r} is neither a local file nor a printer path like /usb/part.bgcode")
        steps = [
            Step("POST", f"/app/printers/{_q(str(printer['uuid']))}/commands/sync",
                 json_body={"command": "START_PRINT", "kwargs": {"path": target}}),
        ]
    return Plan("print", printer, steps, moves_printer=True, refusal=refusal)


def plan_command(printer: dict[str, Any], command: str) -> Plan:
    state = printer_state(printer)
    allowed = COMMAND_STATES[command]
    refusal = None
    if state not in allowed:
        refusal = f"printer {printer.get('name')!r} is {state}; {command} needs {' or '.join(allowed)}. Nothing was sent."
    step = Step("POST", f"/app/printers/{_q(str(printer['uuid']))}/commands/sync",
                json_body={"command": command, "kwargs": {}})
    return Plan(command.lower(), printer, [step], moves_printer=True, refusal=refusal)


def execute(connect: Connect, plan: Plan, progress: Callable[[str], None]) -> dict[str, Any]:
    if plan.refusal:
        raise RefusedError(plan.refusal)
    results: dict[str, Any] = {"action": plan.action, "printer": plan.printer.get("name")}
    upload_id: Any = None
    file_hash: Any = None
    for step in plan.steps:
        query = dict(step.query)
        body = step.json_body
        if "upload_id" in query:
            query["upload_id"] = upload_id
        if isinstance(body, dict) and body.get("hash") == "<hash from upload>":
            if not file_hash:
                raise PrusaConnectError("upload finished without a file hash; not queueing")
            body = {**body, "hash": file_hash}
        progress(f"{step.method} {step.path}")
        if step.upload_file is not None:
            f = step.upload_file
            size = f.stat().st_size
            resp = connect.call(
                step.method, step.path, query,
                raw_body=lambda f=f: f.open("rb"),  # type: ignore[misc]
                headers={"Content-Type": "text/x.gcode", "Content-Length": str(size), "Upload-Size": str(size)},
                timeout=max(120.0, size / 50_000),
            )
            if isinstance(resp, dict):
                file_hash = resp.get("hash") or file_hash
            results["file"] = resp
        else:
            resp = connect.call(step.method, step.path, query or None, json_body=body)
            if step.path.endswith("/uploads") and isinstance(resp, dict):
                upload_id = resp.get("id")
                file_hash = resp.get("hash")
                if upload_id is None:
                    raise PrusaConnectError("upload registration returned no id")
                results["upload"] = resp
            else:
                results.setdefault("responses", []).append(resp)
    return results


def env_connect_url() -> str:
    return os.environ.get("PRUSA_CONNECT_URL", CONNECT_URL)
