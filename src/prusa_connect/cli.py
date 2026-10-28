"""Command-line interface for prusa-connect.

stdout carries only the requested output (tables, or JSON with --json, or a
dry-run plan); progress, prompts, warnings and errors go to stderr. Tokens are
never printed anywhere.

Exit codes: 0 success, 1 the operation failed or a safety guard refused it
(not logged in, API error, printer not READY), 2 usage error or user aborted
(including a printer-moving command without --yes and no terminal to ask on).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from . import __version__, api, auth, store
from .transport import PrusaConnectError, Transport, urllib_transport


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


LOGIN_HELP = """\
Sign in to Prusa Account (one time; the tool refreshes the login by itself afterwards).

  1. Open this address on any device (phone, laptop) and sign in to Prusa Account:

     {url}

  2. When it finishes you land on a connect.prusa3d.com page. Copy the full
     address from the address bar: it contains "code=". If the page already
     moved on, find the ".../login/auth-callback?code=..." entry in the
     browser history.
  3. Paste it here (just the code also works) and press Enter. Do it within a
     minute or two: the code expires quickly.
"""


def _common() -> _ArgumentParser:
    p = _ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="print raw JSON instead of a table")
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                   help="print detailed progress")
    p.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                   help="suppress non-error output")
    return p


def _build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = _ArgumentParser(
        prog="prusa-connect",
        description="Headless command-line client for Prusa Connect (connect.prusa3d.com).",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND", parser_class=_ArgumentParser)
    sub.required = True

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, description=help_, parents=[common])

    p = add("login", "sign in to Prusa Account (paste-back flow that works from any device)")
    p.add_argument("--client", choices=sorted(auth.CLIENTS), default="connect",
                   help="which public Prusa client to sign in as (default: connect)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--start", action="store_true",
                      help="only print the sign-in URL (stdout) and remember the attempt; finish with --finish")
    mode.add_argument("--finish", metavar="URL_OR_CODE",
                      help="complete a --start login with the pasted address ('-' reads it from stdin)")
    mode.add_argument("--status", action="store_true", help="show whether a login is stored (never the token)")

    p = add("logout", "forget the stored login (deletes the local token file)")

    add("whoami", "show the signed-in Prusa Connect user")
    add("printers", "list printers with their state")

    for name, help_ in (("status", "state, temperatures and current job of a printer"),
                        ("queue", "the printer's Prusa Connect print queue"),
                        ("commands", "commands the printer says it supports")):
        p = add(name, help_)
        p.add_argument("printer", nargs="?", help="name, UUID or serial (optional with one printer)")

    p = add("files", "files stored on the printer (or in Connect cloud storage with --cloud)")
    p.add_argument("printer", nargs="?", help="name, UUID or serial (optional with one printer)")
    p.add_argument("--path", help="folder on the printer, e.g. /usb (default: list storages)")
    p.add_argument("--cloud", action="store_true", help="list the team's Connect cloud files instead")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)

    p = add("jobs", "print history, newest first")
    p.add_argument("printer", nargs="?", help="name, UUID or serial (optional with one printer)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--state", action="append", default=[], help="filter, e.g. FIN_OK (repeatable)")

    p = add("upload", "copy a sliced .gcode/.bgcode to the printer (does not start it)")
    p.add_argument("file", type=Path)
    p.add_argument("printer")
    p.add_argument("--destination", help="storage folder on the printer, e.g. /usb (default: first USB)")
    p.add_argument("--filename", help="name to store it under (default: the local name)")
    p.add_argument("--dry-run", action="store_true", help="show the exact requests and send nothing")

    p = add("print", "start a print: a local file (uploaded, then queued first) or a path on the printer")
    p.add_argument("file", help="local .gcode/.bgcode, or a printer path such as /usb/part.bgcode")
    p.add_argument("printer")
    p.add_argument("--destination", help="storage folder for a local file (default: first USB)")
    p.add_argument("--dry-run", action="store_true", help="show the exact requests and send nothing")
    p.add_argument("--yes", action="store_true", help="required (or a typed confirmation): the printer will move")

    for name, help_ in (("pause", "pause the running print"), ("resume", "resume a paused print"),
                        ("stop", "stop (cancel) the current print")):
        p = add(name, help_)
        p.add_argument("printer")
        p.add_argument("--dry-run", action="store_true", help="show the exact request and send nothing")
        p.add_argument("--yes", action="store_true", help="required (or a typed confirmation): the printer will move")

    return parser


def _emit(data: Any, as_json: bool, table: str) -> None:
    if as_json:
        json.dump(data, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(table if table.endswith("\n") else table + "\n")


def _table(rows: list[list[Any]], header: list[str]) -> str:
    cells = [header] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(header))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells)


def _fmt_temp(t: dict[str, Any] | None) -> str:
    if not t:
        return "-"
    a, tgt = t.get("actual"), t.get("target")
    a_s = f"{float(a):.1f}" if isinstance(a, (int, float)) else "?"
    return f"{a_s}°C" + (f" -> {float(tgt):.0f}°C" if isinstance(tgt, (int, float)) and tgt else "")


def _fmt_dur(s: Any) -> str:
    if not isinstance(s, (int, float)):
        return "-"
    s = int(s)
    h, rem = divmod(s, 3600)
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s"


def _status_text(s: dict[str, Any]) -> str:
    lines = [
        f"printer:  {s['name']} ({s.get('model') or '?'})",
        f"uuid:     {s['uuid']}",
        f"state:    {s['state']}",
        f"nozzle:   {_fmt_temp(s.get('nozzle'))}",
        f"bed:      {_fmt_temp(s.get('bed'))}",
    ]
    job = s.get("job")
    if job:
        prog = job.get("progress")
        lines += [
            f"job:      {job.get('file') or '?'} (id {job.get('id')})",
            f"progress: {prog if prog is not None else '?'}%",
            f"elapsed:  {_fmt_dur(job.get('time_printing_s'))}",
            f"left:     {_fmt_dur(job.get('time_remaining_s'))}",
        ]
    else:
        lines.append("job:      none")
    return "\n".join(lines)


def _files_rows(data: Any) -> list[list[Any]]:
    items = data.get("files") or data.get("children") or [] if isinstance(data, dict) else data or []
    rows = []
    for f in items if isinstance(items, list) else []:
        if not isinstance(f, dict):
            continue
        rows.append([f.get("type"), f.get("size"), f.get("display_name") or f.get("name"),
                     f.get("path") or f.get("name")])
    return rows


def _jobs_rows(data: Any) -> list[list[Any]]:
    items = data.get("jobs") if isinstance(data, dict) else data
    rows = []
    for j in items if isinstance(items, list) else []:
        if not isinstance(j, dict):
            continue
        started = j.get("start")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if isinstance(started, (int, float)) else ""
        f: dict[str, Any] = j["file"] if isinstance(j.get("file"), dict) else {}
        rows.append([j.get("id"), j.get("state"), when, _fmt_dur(j.get("time_printing")),
                     f.get("display_name") or j.get("display_name") or str(j.get("path", "")).lstrip("/")])
    return rows


def _confirm(args: argparse.Namespace, what: str, log: Any) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        log(f"prusa-connect: error: refusing to {what} without --yes (the printer will move; stdin is not a TTY)")
        return False
    log(f"About to {what}. The printer will move. Type 'yes' to confirm: ")
    try:
        reply = input()
    except EOFError:
        reply = ""
    if reply.strip().lower() != "yes":
        log("Aborted.")
        return False
    return True


def main(argv: Sequence[str] | None = None, transport: Transport | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"prusa-connect: error: {exc}", file=sys.stderr)
        return 2
    for flag in ("json", "verbose", "quiet"):
        if not hasattr(args, flag):
            setattr(args, flag, False)
    tx = transport or urllib_transport

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    def vlog(message: str) -> None:
        if args.verbose and not args.quiet:
            print(message, file=sys.stderr)

    try:
        return _dispatch(args, tx, log, vlog)
    except api.RefusedError as exc:
        print(f"prusa-connect: refused: {exc}", file=sys.stderr)
        return 1
    except PrusaConnectError as exc:
        print(f"prusa-connect: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("prusa-connect: interrupted", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace, tx: Transport, log: Any, vlog: Any) -> int:
    cmd = args.cmd
    if cmd == "login":
        return _login(args, tx, log)
    if cmd == "logout":
        removed = store.remove(store.token_path())
        store.remove(store.pending_path())
        log("Login removed." if removed else "No login was stored.")
        return 0

    c = api.Connect(tx, api.env_connect_url())

    if cmd == "whoami":
        me = c.whoami()
        user: dict[str, Any] = me["user"] if isinstance(me.get("user"), dict) else me
        _emit(me, args.json, "\n".join(
            f"{k}: {user.get(k)}" for k in ("username", "email", "first_name", "last_name", "id") if user.get(k)
        ) or json.dumps(me))
        return 0
    if cmd == "printers":
        ps = [api.summarize(p) for p in c.printers()]
        _emit(ps, args.json, _table([[p["name"], p["state"], p.get("model"), p["uuid"]] for p in ps],
                                    ["NAME", "STATE", "MODEL", "UUID"]))
        return 0

    printer = c.resolve(getattr(args, "printer", None))
    uuid = str(printer["uuid"])
    vlog(f"printer: {printer.get('name')} ({uuid})")

    if cmd == "status":
        s = api.summarize(c.printer(uuid))
        _emit(s, args.json, _status_text(s))
        return 0
    if cmd == "files":
        if args.cloud:
            data = c.cloud_files(printer.get("team_id"), args.limit, args.offset)
        elif args.path:
            data = c.files(uuid, args.path, args.limit, args.offset)
        else:
            sts = c.storages(uuid)
            _emit(sts, args.json, _table([[s.get("type"), s.get("path") or s.get("mountpoint"),
                                           s.get("free_space"), "ro" if s.get("read_only") else "rw"]
                                          for s in sts], ["TYPE", "PATH", "FREE", "MODE"])
                  + "\n(use --path PATH to list a folder)")
            return 0
        _emit(data, args.json, _table(_files_rows(data), ["TYPE", "SIZE", "NAME", "PATH"]))
        return 0
    if cmd == "jobs":
        data = c.jobs(uuid, args.limit, args.offset, args.state)
        _emit(data, args.json, _table(_jobs_rows(data), ["ID", "STATE", "STARTED", "PRINTED", "FILE"]))
        return 0
    if cmd == "queue":
        data = c.queue(uuid)
        _emit(data, True, "")
        return 0
    if cmd == "commands":
        data = c.supported_commands(uuid)
        items = data.get("commands") if isinstance(data, dict) else data
        rows = [[x.get("command"), ",".join(x.get("executable_from_state") or [])]
                for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        _emit(data, args.json, _table(rows, ["COMMAND", "ALLOWED IN STATES"]))
        return 0

    # --- write commands ----------------------------------------------------
    # Always re-read the single printer record: the list can lag behind.
    printer = {**printer, **c.printer(uuid)}
    if cmd == "upload":
        plan = api.plan_upload(c, printer, args.file.expanduser(), args.destination, args.filename)
    elif cmd == "print":
        plan = api.plan_print(c, printer, args.file, args.destination)
    else:
        plan = api.plan_command(printer, {"pause": "PAUSE_PRINT", "resume": "RESUME_PRINT",
                                          "stop": "STOP_PRINT"}[cmd])

    if args.dry_run:
        json.dump(plan.describe(c), sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        if plan.refusal:
            log(f"prusa-connect: dry run: a real run would be REFUSED: {plan.refusal}")
            return 1
        log("prusa-connect: dry run: nothing was sent.")
        return 0

    if plan.refusal:
        raise api.RefusedError(plan.refusal)
    if plan.moves_printer and not _confirm(args, f"{cmd} on {printer.get('name')!r}", log):
        return 2
    result = api.execute(c, plan, vlog)
    _emit(result, True, "")
    log(f"prusa-connect: {cmd} sent to {printer.get('name')}.")
    return 0


def _login(args: argparse.Namespace, tx: Transport, log: Any) -> int:
    if args.status:
        info = auth.login_info()
        _emit(info, args.json, "\n".join(f"{k}: {v}" for k, v in info.items()))
        return 0 if info["logged_in"] else 1
    if args.finish is not None:
        pasted = sys.stdin.readline() if args.finish == "-" else args.finish
        auth.finish_login(pasted, tx)
        log(f"Logged in. Token stored in {store.token_path()} (mode 0600).")
        return 0
    url = auth.start_login(auth.CLIENTS[args.client])
    if args.start:
        sys.stdout.write(url + "\n")
        log("Open that URL, sign in, then run: prusa-connect login --finish '<address you landed on>'")
        return 0
    print(LOGIN_HELP.format(url=url), file=sys.stderr)
    if args.client == "slicer":
        log("  (slicer client: the browser is sent to prusaslicer://login?code=...; the address is only "
            "visible in developer tools > Network, on the last /o/authorize/ response's Location header.)")
    print("Paste here: ", end="", file=sys.stderr, flush=True)
    try:
        pasted = input()
    except EOFError:
        log("prusa-connect: error: nothing was pasted")
        return 2
    auth.finish_login(pasted, tx)
    log(f"Logged in. Token stored in {store.token_path()} (mode 0600).")
    return 0
