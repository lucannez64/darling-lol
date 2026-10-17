#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
state_dir=${XDG_STATE_HOME:-"$HOME/.local/state"}/darling-lol
mkdir -p "$state_dir"
log_file=$state_dir/launcher.log

{
  printf '\n[%s] Starting League of Legends\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
} >>"$log_file"
exec >>"$log_file" 2>&1
exec "$repo/scripts/run.sh"
