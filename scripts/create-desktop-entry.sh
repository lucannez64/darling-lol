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
# Match the install mode even when recreating shortcuts from the host.
recorded_toolbox=
if [ -r build/toolbox-container ]; then
  IFS= read -r recorded_toolbox < build/toolbox-container || true
fi
set --
if [ -f /run/.toolboxenv ] || [ "${DARLING_LOL_TOOLBOX:-0}" = 1 ]; then
  name=${DARLING_LOL_TOOLBOX_NAME:-$recorded_toolbox}
  if [ -z "$name" ]; then
    # Keep the previous entry as a fallback for manually entered containers
    # and checkouts created before the installer wrote a marker.
    name=$(sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' \
      "${HOME:-}/.local/share/applications/darling-launcher.desktop" 2>/dev/null | head -n 1)
  fi
  set -- --toolbox "${name:-darling-lol}"
elif [ -n "$recorded_toolbox" ] && [ ! -f build/native-install ]; then
  # Checking Podman storage recognizes stopped containers without starting one.
  if command -v toolbox >/dev/null 2>&1 &&
     podman container exists -- "$recorded_toolbox" >/dev/null 2>&1; then
    set -- --toolbox "$recorded_toolbox"
  else
    printf 'Recorded Toolbox container %s is unavailable. Creating a host shortcut.\n' \
      "$recorded_toolbox" >&2
  fi
fi
if [ -n "$window_entries" ]; then
  set -- "$@" "$window_entries"
fi
python3 build/runtime/tools/desktop-entry.py "$@"
