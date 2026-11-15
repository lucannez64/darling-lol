#!/bin/sh
# Menu launcher entry point. When the Riot Client is missing, run the
# installer with the configured region instead of failing; when it exists,
# hand off to desktop-run.sh unchanged. A menu launch has no terminal, so all
# diagnostics land in build/desktop-launch.log (shared with desktop-run.sh).
set -eu
umask 077
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$repo/build"
log="$repo/build/desktop-launch.log"
touch "$log"
chmod 600 "$log"
exec >>"$log" 2>&1

# The prefix the launcher itself uses. Like run.sh, refresh the configured
# tools, then let runtime-paths.sh answer: it honors DPREFIX and otherwise
# uses the prefix recorded by `configure.py --prefix` in build/config.json.
# Without a known prefix the installer must not run, because it could then
# start over an existing install.
if ! python3 "$repo/scripts/configure.py" >/dev/null ||
   ! prefix=$(. "$repo/build/runtime/tools/runtime-paths.sh" >/dev/null &&
              printf '%s' "$prefix_dir") || [ -z "$prefix" ]; then
  echo 'Could not resolve the Darling prefix from build/config.json; not starting the Riot Client installer.'
  exit 1
fi
client="$prefix/Applications/Riot Client.app"
shared="$prefix/Users/Shared/Riot Games/Riot Client.app"
if [ -e "$client" ] || [ -d "$shared" ]; then
  exec "$repo/scripts/desktop-run.sh" "$@"
fi

# First launch without a client: install it with the region chosen during the
# Darling-LoL install. installer.sh starts the client itself when it finishes,
# so no extra hand-off is needed here.
region=$(python3 - "$repo/build/config.json" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("region", "EUW").lower())
except Exception:
    print("euw")
PY
) || region=euw

# Surface the long installation through the desktop notification service when
# it is reachable. Toolbox containers share the host session bus and Fedora
# toolbox images ship libnotify; elsewhere this degrades to silence and the
# log remains the only progress source.
if command -v notify-send >/dev/null 2>&1; then
  DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-"unix:path=/run/user/$(id -u)/bus"}
  export DBUS_SESSION_BUS_ADDRESS
  notify-send -a 'Darling-LoL' 'Riot Client not found' \
    "Installing it for region ${region}." \
    2>/dev/null || true
fi

echo "Riot Client is not installed in $prefix; launching scripts/installer.sh $region"
exec "$repo/scripts/installer.sh" "$region"
