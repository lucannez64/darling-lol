#!/bin/sh
# Install the corrected legacy system-version API into the stopped guest prefix.
# Exit the game and both clients, then run darling shutdown before this script.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$project_dir/tools/runtime-paths.sh"
artifact=${RIOT_CARBONCORE_ARTIFACT:-$project_dir/build/riot-CarbonCore}
backup_dir=${RIOT_CARBONCORE_BACKUP_DIR:-$project_dir/build/carboncore-runtime-backups}
sdk_framework="@DARLING_SDK@/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/CarbonCore.framework"
framework="$guest_home/RuntimeFrameworks/CarbonCore.framework"
prefix_binary="$prefix_dir/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/CarbonCore.framework/Versions/A/CarbonCore"
[ -f "$artifact" ] || { echo "Build CarbonCore first: $artifact" >&2; exit 1; }
[ -f "$sdk_framework/Versions/A/CarbonCore" ] || { echo "Missing SDK CarbonCore framework" >&2; exit 1; }
[ "${RCLOL_RUNTIME_STOPPED:-0}" = 1 ] || python3 "$project_dir/tools/stop-darling-runtime.py"
mkdir -p "$guest_home/RuntimeFrameworks" "$backup_dir"
if [ ! -d "$framework" ]; then
  cp -a "$sdk_framework" "$framework"
fi
for binary in "$framework/Versions/A/CarbonCore" "$prefix_binary"; do
  if [ -L "$binary" ]; then echo "Expected a regular binary: $binary" >&2; exit 1; fi
  if [ -f "$binary" ] && cmp -s "$artifact" "$binary"; then continue; fi
  previous="$binary"
  if [ ! -f "$previous" ]; then previous="$sdk_framework/Versions/A/CarbonCore"; fi
  old_hash=$(sha256sum "$previous" | cut -d ' ' -f 1)
  backup="$backup_dir/CarbonCore-$old_hash"
  if [ -e "$backup" ]; then
    cmp -s "$previous" "$backup" || { echo "Backup verification failed: $backup" >&2; exit 1; }
  else
    cp -p "$previous" "$backup"
  fi
  mkdir -p "$(dirname -- "$binary")"
  staged=$(mktemp "$binary.staged.XXXXXX")
  trap 'rm -f "$staged"' EXIT HUP INT TERM
  install -m755 "$artifact" "$staged"
  cmp -s "$artifact" "$staged" || { echo "Staged CarbonCore verification failed" >&2; exit 1; }
  mv -f "$staged" "$binary"
done
echo "CarbonCore installed; backups: $backup_dir"
echo "Launch scripts/run.sh to activate it."
