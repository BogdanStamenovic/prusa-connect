"""Where prusa-connect keeps its login, and how it writes it.

Tokens are bearer credentials for the whole Prusa account, so every file here
is written 0600 inside a 0700 directory, atomically (temp file + rename), and
never echoed. The directory lives outside the ownbox checkout on purpose: an
`ownbox update` or reinstall replaces the checkout, and the login must survive.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

APP = "prusa-connect"


def config_dir() -> Path:
    override = os.environ.get("PRUSA_CONNECT_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def token_path() -> Path:
    return config_dir() / "tokens.json"


def pending_path() -> Path:
    return config_dir() / "pending-login.json"


def lock_path() -> Path:
    return config_dir() / ".lock"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def remove(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Serialize token refreshes across processes.

    Prusa Account rotates the refresh token on every use, so two agents
    refreshing at once would leave one of them holding a dead token. Whoever
    takes the lock second re-reads the file and finds the fresh pair.
    """
    _ensure_dir(config_dir())
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
