#!/usr/bin/env bash
# Fresh clone -> working `prusa-connect`. Safe to rerun (ownbox falls back to it).
set -euo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1
cd "${OWNBOX_TOOL_DIR:-$(dirname "$0")/..}"

PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
# A venv made by `uv venv` has no pip; give it one rather than failing.
.venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip >/dev/null
.venv/bin/python -m pip install -q -e .

# The login lives outside the checkout so reinstalling or updating never touches it.
cfg="${PRUSA_CONNECT_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/prusa-connect}"
mkdir -p "$cfg"
chmod 700 "$cfg"

.venv/bin/prusa-connect --version
if .venv/bin/prusa-connect -q login --status >/dev/null 2>&1; then
  echo "prusa-connect: a stored login was found in $cfg and kept."
else
  echo "prusa-connect: not logged in yet. Run: prusa-connect login"
fi
