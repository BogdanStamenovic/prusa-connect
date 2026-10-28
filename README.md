# prusa-connect

A headless command-line client for [Prusa Connect](https://connect.prusa3d.com): sign in once from
any device, then read printer state, temperatures, job progress, stored files and print history,
and (behind explicit guards) upload files and start, pause, resume or stop prints. No desktop,
browser automation or PrusaSlicer needed on the machine running it. Standard library only.

## How it works

- **Auth.** Prusa Account is a Django OAuth2 server (`account.prusa3d.com/o/authorize/`,
  `/o/token/`). Prusa publishes no third-party client, so `login` uses the public client of the
  Connect web app (`ACCOUNT_CLIENT_ID` in `https://connect.prusa3d.com/environment.js`) with
  PKCE S256. Its redirect is an ordinary https page, so after signing in the code is in the
  browser's address bar on any phone or laptop; you paste it back. `--client slicer` uses
  PrusaSlicer's client instead (`m_account_client_id` in PrusaSlicer's `ServiceConfig.cpp`,
  also in the 2.9.6 binary), whose `prusaslicer://login?code=` redirect is only visible in
  browser developer tools. It is there as a fallback.
- **Tokens** go to `~/.config/prusa-connect/tokens.json` (`$XDG_CONFIG_HOME` honoured,
  `$PRUSA_CONNECT_HOME` overrides), mode 0600 in a 0700 directory, written atomically. They are
  never printed; dry-runs show `Bearer <stored token, not shown>`. The access token is refreshed
  automatically when it is within 2 minutes of expiry, or after a 401. Prusa rotates refresh
  tokens on every use, so refreshes take an `flock` and re-read the file; two agents refreshing
  at once do not knock each other out.
- **API.** Connect's `/app/...` REST API is undocumented. Paths and payloads are taken from what
  PrusaSlicer sends (upload: `POST /app/users/teams/{team}/uploads`, then
  `PUT /app/teams/{team}/files/raw?upload_id=`) and what the Connect web app sends
  (`POST /app/printers/{uuid}/commands/sync` with `{"command", "kwargs"}`).
- **Writes are plans.** Every write command first builds the exact list of requests, then either
  prints it (`--dry-run`) or runs that same list.

## Install

```bash
ownbox install prusa-connect
prusa-connect login
```

`ownbox update prusa-connect` keeps the login (it lives outside the checkout).
`ownbox uninstall prusa-connect` asks whether to delete the stored login; with no terminal it
keeps it and says so (`PRUSA_CONNECT_REMOVE_LOGIN=1` deletes it).

Manual:

```bash
git clone https://github.com/BogdanStamenovic/prusa-connect && cd prusa-connect
bash deploy/setup.sh            # or: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Usage

| Command | What it does | Sends to printer? |
|---|---|---|
| `login` | interactive: prints a URL, reads the pasted callback address | no |
| `login --start` / `login --finish URL` | the same split in two, for relaying through someone else (`--finish -` reads stdin) | no |
| `login --status` | whether a login is stored, how long the access token has left (exit 1 if none) | no |
| `logout` | deletes the local token file | no |
| `whoami` | the signed-in Connect user | no |
| `printers` | name, state, model, UUID | no |
| `status [PRINTER]` | state, nozzle/bed temperature, job, progress, elapsed, time left | no |
| `files [PRINTER] [--path /usb] [--cloud]` | storages, a folder's files, or Connect cloud files | no |
| `jobs [PRINTER] [--state FIN_OK]` | print history, newest first | no |
| `queue [PRINTER]`, `commands [PRINTER]` | Connect queue; supported printer commands | no |
| `upload FILE PRINTER` | copy a `.gcode`/`.bgcode` to the printer, does not start it | file only |
| `print FILE PRINTER --yes` | local file: upload, then put it first in the queue. Printer path (`/usb/x.bgcode`): `START_PRINT` | **yes** |
| `pause` / `resume` / `stop PRINTER --yes` | `PAUSE_PRINT` / `RESUME_PRINT` / `STOP_PRINT` | **yes** |

`PRINTER` is a name, UUID or serial, case-insensitive; a unique substring of the name works. It
may be left out of read commands when the account has one printer. Write commands require it.

Global flags: `--json` (raw JSON on stdout), `-v`, `-q`, `--version`.

### Guards

- **`print` refuses unless the printer is `READY`.** In Connect, READY means a human confirmed
  the bed is clear ("Set ready" in Connect, or on the printer). IDLE, FINISHED and STOPPED are
  refused because the last part may still be on the plate. The tool never sends `set_ready`.
- `pause` needs PRINTING, `resume` needs PAUSED, `stop` needs PRINTING, PAUSED or ATTENTION.
- Anything that moves the printer (`print`, `pause`, `resume`, `stop`) needs `--yes`, or a typed
  `yes` on a terminal. With no terminal and no `--yes` it exits 2 and sends nothing.
- `--dry-run` on every write command prints the plan (method, URL, JSON body, file size) to
  stdout and sends nothing. It does make read-only GETs to resolve the printer and its state,
  and its exit code matches what the real run would do (1 if a guard would refuse).
- `upload` only accepts `.gcode` / `.bgcode`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | failed: not logged in, API or network error, or a guard refused (nothing sent) |
| 2 | usage error, or aborted (no `--yes` and no terminal, or confirmation not given) |

stdout is only the requested output; progress, prompts and errors are on stderr.

### Examples

```bash
prusa-connect login --start            # agent: prints the URL; a human opens it anywhere
prusa-connect login --finish 'https://connect.prusa3d.com/login/auth-callback?code=...&state=...'
prusa-connect printers
prusa-connect status "Core One" --json
prusa-connect files --path /usb
prusa-connect print ~/parts/clip.bgcode "Core One" --dry-run
prusa-connect pause "Core One" --yes
```

## Limitations

- **Borrowed OAuth client.** Both client ids belong to Prusa's own apps. If Prusa restricts them,
  login breaks until the tool is updated. The default flow depends on Connect's callback page
  leaving a code alone when it did not issue the `state`; that is how it behaved for prusactl as
  of 2026, and it was not verified here before the first real login.
- **The code expires fast** (Django OAuth Toolkit's default is 60 s; Prusa's setting is not
  known). Relaying the pasted URL through a chat can be too slow; then run `login --start` again.
  Our side of a `--start` login stays valid for 6 hours (the verifier is useless without a fresh code), so only the step from signing in to pasting is time-critical.
- **Undocumented API.** Field names (`connect_state`, `job_info.time_remaining`, ...) were taken
  from PrusaSlicer source and community clients. A rename shows up as missing values or a clear
  "API changed" error, not a silent wrong action.
- **Write paths have never been run against a real printer.** They are covered by unit tests
  against a fake HTTP layer only, by design (nothing was to be sent to the printer while this
  was built). The `print` path for a local file (upload, then queue position 0) relies on Connect
  starting the queue head on a READY printer once the transfer finishes. That is untested.
- No camera snapshots, no firmware updates, no queue editing, no file deletion, no G-code
  commands, no setting the printer READY. Those are deliberately left out.
- No password login: the password never passes through this tool.
- Linux and macOS only (`fcntl` locking).

## Prior art

[trevin-lee/prusactl](https://github.com/trevin-lee/prusactl) (Go, CLI + MCP, PrusaLink and
Connect) and [gioelemo/prusa-mcp](https://github.com/gioelemo/prusa-mcp) informed the client
ids and endpoints used here.
