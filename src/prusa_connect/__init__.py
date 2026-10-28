"""prusa-connect: headless command-line client for Prusa Connect."""

from __future__ import annotations

__version__ = "0.1.0"

from .api import APIError, Connect, RefusedError
from .auth import AuthError
from .cli import main
from .transport import PrusaConnectError

__all__ = [
    "APIError",
    "AuthError",
    "Connect",
    "PrusaConnectError",
    "RefusedError",
    "__version__",
    "main",
]
