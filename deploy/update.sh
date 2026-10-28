#!/usr/bin/env bash
# Upgrade in place. The token file is never read or rewritten here; it lives in
# the config dir, not the checkout, so a pull or rollback cannot lose it.
# Idempotent and rollback-safe: the token format has no migrations (an older
# revision reads the same JSON keys).
set -euo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1
cd "${OWNBOX_TOOL_DIR:-$(dirname "$0")/..}"

PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys' 2>/dev/null; then
  rm -rf .venv
  "$PY" -m venv .venv
fi
# A venv made by `uv venv` has no pip; give it one rather than failing.
.venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip >/dev/null
.venv/bin/python -m pip install -q -e .
.venv/bin/prusa-connect --version
if .venv/bin/prusa-connect -q login --status >/dev/null 2>&1; then
  echo "prusa-connect: stored login preserved."
else
  echo "prusa-connect: no stored login (run: prusa-connect login)."
fi
