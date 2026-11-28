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
recorded_backend=toolbox
if [ -r build/toolbox-backend ]; then
  IFS= read -r backend_value < build/toolbox-backend || true
  if [ "$backend_value" = distrobox ]; then
    recorded_backend=distrobox
  fi
fi
set --
# Distrobox comes first: its init also creates /run/.toolboxenv, so only
# CONTAINER_ID (set by distrobox create/enter) tells the two apart.
if [ -n "${CONTAINER_ID:-}" ] && [ -f "${DARLING_CONTAINER_ENV:-/run/.containerenv}" ]; then
  backend=${DARLING_LOL_TOOLBOX_BACKEND:-distrobox}
  name=${DARLING_LOL_TOOLBOX_NAME:-$CONTAINER_ID}
  if [ "$backend" = distrobox ]; then
    set -- --distrobox "$name"
  else
    set -- --toolbox "$name"
  fi
elif [ -f "${DARLING_TOOLBOX_ENV:-/run/.toolboxenv}" ]; then
  backend=${DARLING_LOL_TOOLBOX_BACKEND:-toolbox}
  name=${DARLING_LOL_TOOLBOX_NAME:-$recorded_toolbox}
  if [ -z "$name" ]; then
    # Keep the previous entry as a fallback for manually entered containers
    # and checkouts created before the installer wrote a marker.
    entry="${HOME:-}/.local/share/applications/darling-launcher.desktop"
    name=$(sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' \
      "$entry" 2>/dev/null | head -n 1)
  fi
  if [ "$backend" = distrobox ]; then
    set -- --distrobox "${name:-darling-lol}"
  else
    set -- --toolbox "${name:-darling-lol}"
  fi
elif [ "${DARLING_LOL_TOOLBOX:-0}" = 1 ]; then
  backend=${DARLING_LOL_TOOLBOX_BACKEND:-$recorded_backend}
  name=${DARLING_LOL_TOOLBOX_NAME:-$recorded_toolbox}
  if [ -z "$name" ]; then
    entry="${HOME:-}/.local/share/applications/darling-launcher.desktop"
    name=$(sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' \
      "$entry" 2>/dev/null | head -n 1)
    if [ -z "$name" ]; then
      name=$(sed -n 's/^Exec=\(env DBX_CONTAINER_MANAGER=podman \)\{0,1\}distrobox enter --no-tty --name \([^ ]*\) -- .*$/\2/p' \
        "$entry" 2>/dev/null | head -n 1)
      if [ -n "$name" ] && [ -z "${DARLING_LOL_TOOLBOX_BACKEND:-}" ]; then
        backend=distrobox
      fi
    fi
  fi
  if [ "$backend" = distrobox ]; then
    set -- --distrobox "${name:-darling-lol}"
  else
    set -- --toolbox "${name:-darling-lol}"
  fi
elif [ -n "$recorded_toolbox" ] && [ ! -f build/native-install ]; then
  # Checking Podman storage recognizes stopped containers without starting one.
  if [ "$recorded_backend" = distrobox ]; then
    if command -v distrobox >/dev/null 2>&1 &&
       podman container exists -- "$recorded_toolbox" >/dev/null 2>&1; then
      set -- --distrobox "$recorded_toolbox"
    else
      printf 'Recorded Distrobox container %s is unavailable. Creating a host shortcut.\n' \
        "$recorded_toolbox" >&2
    fi
  elif command -v toolbox >/dev/null 2>&1 &&
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
