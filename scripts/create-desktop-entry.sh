#!/bin/sh
# Create/update GNOME desktop entry and launcher icon.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
python3 scripts/configure.py >/dev/null
# Match the install mode: an entry created inside Toolbox must re-enter the
# container, because the host has no Darling build dependencies.
if [ -f /run/.toolboxenv ] || [ "${DARLING_LOL_TOOLBOX:-0}" = 1 ]; then
  set -- --toolbox "${DARLING_LOL_TOOLBOX_NAME:-darling-lol}"
fi
python3 build/runtime/tools/desktop-entry.py "$@"
