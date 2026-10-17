#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
region=${1:-euw}
case "$region" in euw|eune|na|br|kr|jp|la1|la2|oce|ru|tr) ;; *) echo 'Use a Riot installer region code' >&2; exit 1;; esac
python3 scripts/configure.py >/dev/null
bash build/runtime/tools/fetch-installer.sh "$region" build/installer
unzip -q -o "build/installer/live.$region.zip" -d build/installer
RIOT_LAUNCHER_HOST="$repo/build/installer/Install League of Legends $region.app/Contents/MacOS/RiotClientServices"
RCLOL_REGION=$(printf '%s' "$region" | tr '[:lower:]' '[:upper:]')
export RIOT_LAUNCHER_HOST RCLOL_REGION
# The installer exits after installing the Riot Client; its own hand-off to the
# client fails under Darling, so start the installed client here.
sh scripts/run.sh || true
unset RIOT_LAUNCHER_HOST
prefix=${DPREFIX:-$HOME/.darling}
if [ ! -d "$prefix/Users/Shared/Riot Games/Riot Client.app/Contents/MacOS" ]; then
 echo "The installer exited without installing the Riot Client. Its logs are in" >&2
 echo "$prefix/Users/<guest user>/Library/Logs/Riot Games/Install League of Legends $region/Launcher" >&2
 exit 1
fi
exec sh scripts/run.sh
