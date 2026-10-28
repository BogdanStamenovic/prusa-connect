#!/usr/bin/env bash
# Undo setup. The checkout and .venv are removed by ownbox itself; what is left
# is the config dir holding the login. Deleting it is optional: asked on a
# terminal, kept (and said so) without one.
set -euo pipefail
cfg="${PRUSA_CONNECT_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/prusa-connect}"

if [ ! -d "$cfg" ]; then
  echo "prusa-connect: no config dir at $cfg; nothing to remove."
  exit 0
fi

answer=""
if [ -t 0 ]; then
  printf 'Delete the stored Prusa login in %s? [y/N] ' "$cfg"
  read -r answer || answer=""
elif [ "${PRUSA_CONNECT_REMOVE_LOGIN:-}" = "1" ]; then
  answer=y
fi

case "$answer" in
  y|Y|yes|YES)
    rm -rf -- "$cfg"
    echo "prusa-connect: deleted $cfg."
    ;;
  *)
    echo "prusa-connect: kept $cfg (stored login). Delete it by hand, or rerun with PRUSA_CONNECT_REMOVE_LOGIN=1."
    ;;
esac
