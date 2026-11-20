#!/bin/sh
# Create/update GNOME desktop entry and launcher icon.
# --window-entries (or DARLING_LOL_WINDOW_ENTRIES=1) also creates the hidden
# entries that give the League client, game and Riot Client windows a name and
# icon.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
window_entries=
if [ "${DARLING_LOL_WINDOW_ENTRIES:-0}" = 1 ]; then window_entries=--window-entries; fi
for arg in "$@"; do
  case $arg in
    --window-entries) window_entries=--window-entries ;;
    *) echo "create-desktop-entry.sh: unknown option: $arg" >&2; exit 2 ;;
  esac
done
python3 scripts/configure.py >/dev/null
# Match the install mode: an entry created inside Toolbox must re-enter the
# container, because the host has no Darling build dependencies.
if [ -f /run/.toolboxenv ] || [ "${DARLING_LOL_TOOLBOX:-0}" = 1 ]; then
  name=${DARLING_LOL_TOOLBOX_NAME:-}
  if [ -z "$name" ]; then
    # Retain the container recorded by a previously installed entry; fall back
    # to the installer default only when no entry exists yet.
    name=$(sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' \
      "${HOME:-}/.local/share/applications/darling-launcher.desktop" 2>/dev/null | head -n 1)
  fi
  set -- --toolbox "${name:-darling-lol}"
else
  set --
fi
if [ -n "$window_entries" ]; then
  set -- "$@" "$window_entries"
fi
python3 build/runtime/tools/desktop-entry.py "$@"
