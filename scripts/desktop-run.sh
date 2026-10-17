#!/bin/sh
# Preserve diagnostics when a menu launcher has no terminal attached.
set -eu
umask 077
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$repo/build"
log="$repo/build/desktop-launch.log"
touch "$log"
chmod 600 "$log"
exec >>"$log" 2>&1
exec "$repo/scripts/run.sh" "$@"
