"""The one place prusa-connect touches the network.

Everything goes through a `Transport` callable so tests can swap in a fake
and nothing in the test suite can reach Prusa's servers, let alone a printer.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO

from . import __version__

USER_AGENT = f"prusa-connect/{__version__} (+https://github.com/BogdanStamenovic/prusa-connect)"


class PrusaConnectError(Exception):
    """Raised when prusa-connect cannot complete an operation."""


class NetworkError(PrusaConnectError):
    """The request never got an HTTP answer."""


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | BinaryIO | None = None
    timeout: float = 30.0


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[Request], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_transport(req: Request) -> Response:
    headers = {"User-Agent": USER_AGENT, **req.headers}
    r = urllib.request.Request(req.url, data=req.body, headers=headers, method=req.method)
    try:
        with _opener.open(r, timeout=req.timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NetworkError(f"{req.method} {req.url}: {reason}") from exc
