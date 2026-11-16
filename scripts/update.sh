#!/bin/sh
# Bring an existing build and installation up to date after `git pull`: re-apply the patches that changed
# (prepare.py reverts the earlier version it finds applied), rebuild incrementally, and reinstall the base
# runtime, and the per-user overrides. Run it after manually closing Riot/League.
# Rootless callers must remain in their original namespace; the outer launcher
# refuses a live prefix marker instead of trying to reconnect or stop a session.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
# Older setups shipped one serial and UUID shared by every install. Give a prefix that still
# has them its own identity; any other saved values, and prefixes without them, are left alone.
replace_shared_identity() {
  python3 build/runtime/tools/stop-darling-runtime.py
  python3 tools/configure-darling-platform.py --replace-known-defaults --write || \
    echo 'Could not replace the shared platform identity; run tools/configure-darling-platform.py --provision --write' >&2
}
if [ -n "${DARLING_WORKSPACE:-}" ]; then
  python3 scripts/nix-idle.py
  # Initial setup and upgrades use the same supported user-owned install path.
  # Keep each package closure rooted: copied ELF files can retain its loader.
  package=${DARLING_PACKAGE_ID%/share/darling-lol}
  case "$package" in /nix/store/*) ;; *) echo 'Missing Nix package identity' >&2; exit 1;; esac
  mkdir -p build/nix-gcroots
  nix-store --realise "$package" --add-root "$repo/build/nix-gcroots/$(basename "$package")" --indirect >/dev/null
  python3 scripts/nix-state.py invalidate-base
  ./scripts/build.sh
  python3 build/runtime/tools/stop-darling-runtime.py
  cmake --install build/darling-src/build
  python3 scripts/nix-state.py stamp-base
  ./scripts/install-runtime.sh
  replace_shared_identity
  echo 'Rootless update installed. In this shell use scripts/run.sh; otherwise exit this shell and start darling-lol.'
  exit 0
fi

# A Toolbox install builds against the container's libraries and installs the base
# runtime into the container's /usr/local, so the update must run in there too.
# Re-exec when running on the host; the container name comes from the marker
# written by install-native.sh, from the installed desktop entry, or from the
# installer default confirmed by finding Darling inside that container.
if [ ! -f /run/.toolboxenv ]; then
  if [ -f build/toolbox-container ]; then
    name=$(cat build/toolbox-container)
  else
    name=$(sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' \
      "${HOME:-}/.local/share/applications/darling-launcher.desktop" 2>/dev/null | head -n 1)
  fi
  if [ -z "$name" ] && command -v toolbox >/dev/null 2>&1 && \
     toolbox run -c darling-lol sh -c 'command -v darling' >/dev/null 2>&1; then
    # No explicit record, but the installer's default container holds Darling.
    name=darling-lol
  fi
  if [ -n "$name" ]; then
    if ! command -v toolbox >/dev/null 2>&1 || ! toolbox run -c "$name" true 2>/dev/null; then
      echo "This checkout was installed via Toolbox container '$name', but that container is not" >&2
      echo 'reachable. Start it and run the update inside it: toolbox enter '"$name" >&2
      exit 1
    fi
    echo "Toolbox install detected; running the update inside container '$name'..."
    exec toolbox run --container "$name" sh "$repo/scripts/update.sh"
  fi
fi

if [ ! -d build/darling-src/build ]; then
	echo 'No existing build; follow the first-time setup in README.md instead.' >&2
	exit 1
fi
# The install steps shut Darling down and stop leftover Riot processes, so refuse rather than end a session.
python3 - <<'PY'
from pathlib import Path
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        command_line = (entry / 'cmdline').read_bytes()
    except OSError:
        continue
    if b'/Riot Client.app/' in command_line or b'/League of Legends.app/' in command_line:
        raise SystemExit('A Riot Client or League process is running; exit the game and both clients, then run this again.')
PY
./scripts/build.sh
python3 build/runtime/tools/stop-darling-runtime.py
sudo cmake --install build/darling-src/build
./scripts/install-runtime.sh
replace_shared_identity
echo 'Update installed. Start Darling with: sh scripts/run.sh'
