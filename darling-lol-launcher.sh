#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${XDG_DATA_HOME:-$HOME/.local/share}/darling-lol"
SHARE_DIR="@SHARE_DIR@"
FHS_BIN="@FHS_BIN@"
UNSHARE_BIN="@UNSHARE_BIN@"
# The store package is both a compatibility identity and a closure GC root.
export DARLING_PACKAGE_ID="$SHARE_DIR"
export DISPLAY="${DISPLAY:-:0}"
export EGL_PLATFORM="${EGL_PLATFORM:-x11}"
# The guest prefix: DPREFIX if set, else the one saved by --set-prefix, else ~/.darling.
if [ -z "${DPREFIX:-}" ] && [ -f "$WORKSPACE/build/config.json" ]; then
  DPREFIX=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("prefix", ""))' \
    "$WORKSPACE/build/config.json" 2>/dev/null) || DPREFIX=
fi
export DPREFIX="${DPREFIX:-$HOME/.darling}"
# Read-only host tools. They run here rather than in the namespace so they can
# see the real process table, kernel messages and display, and so a crash report
# still works while a session holds the workspace lock.
host_tool() {
  local tool="$SHARE_DIR/tools/$1.py"
  shift
  [ -f "$tool" ] || tool="$(dirname -- "$0")/tools/$(basename -- "${tool}")"
  exec python3 "$tool" "$@"
}
case "${1:-}" in
  --doctor) shift; host_tool doctor "$@";;
  --report) shift; host_tool crash-report "$@";;
  --perf) shift; host_tool perf-baseline "$@";;
esac
if [ "${1:-}" = --set-prefix ]; then
  [ $# -eq 2 ] || { echo 'Usage: darling-lol --set-prefix PATH' >&2; exit 2; }
  # Resolve here: the namespace starts in the workspace, not in this directory.
  set -- --set-prefix "$(realpath -m -- "$2")"
fi
export DARLING_HOST_UID="$(id -u)"
export DARLING_HOST_GID="$(id -g)"
export DARLING_WORKSPACE="$WORKSPACE"

python3 - "$WORKSPACE" <<'PY'
import pathlib, sys
workspace = pathlib.Path(sys.argv[1])
for path in [workspace, *workspace.parents]:
    if path.is_symlink():
        raise SystemExit(f'Refusing symlink workspace ancestor: {path}')
if (workspace / '.launcher.lock').is_symlink():
    raise SystemExit('Refusing symlink workspace lock')
PY
mkdir -p "$WORKSPACE"
if [ "${1:-}" = --stop ]; then
  exec python3 "$SHARE_DIR/scripts/nix-stop.py" "$WORKSPACE" "$DPREFIX"
fi
# One mutable base runtime per workspace; do not refresh scripts mid-session.
exec 9<>"$WORKSPACE/.launcher.lock"
flock -n 9 || { echo 'Another darling-lol workspace session is active; use its terminal.' >&2; exit 1; }
# Do not attempt to reconnect across separately created user namespaces. No kill,
# sudo, or shutdown is attempted here (the PID might even be stale/reused).
if [ -f "$DPREFIX/.init.pid" ]; then
  read -r server_pid < "$DPREFIX/.init.pid" || true
  if [[ "${server_pid:-}" =~ ^[1-9][0-9]*$ ]] && [ -d "/proc/$server_pid" ]; then
    state=$(awk '/^State:/ {print $2}' "/proc/$server_pid/status")
    if [ "$state" != Z ]; then
      echo 'Prefix has a live server marker; namespace reconnect is unsupported.' >&2
      echo 'Run darling-lol --stop to stop it, then retry. No processes were stopped.' >&2
      exit 1
    fi
  fi
fi
python3 "$SHARE_DIR/scripts/nix-copy.py" "$SHARE_DIR" "$WORKSPACE"
mkdir -p "$WORKSPACE/usr-local"
cd "$WORKSPACE"
# Map subordinate IDs before bubblewrap sets no_new_privs. UID 0 is not host root.
fhs_command=("$UNSHARE_BIN" --user --map-auto --map-current-user --setuid=0 --setgid=0 "$FHS_BIN")
stop_request="$WORKSPACE/.stop-request"
rm -f "$stop_request"
# Ctrl+C, a closed terminal or SIGTERM must reach nix-lifecycle.py, which shuts
# Darling down in its namespace. They would kill bubblewrap first, and
# bubblewrap's --die-with-parent would kill the lifecycle with it. So bubblewrap
# starts with them ignored and env restores them inside. This supervisor turns
# them into a stop request, the same one darling-lol --stop makes.
session() {
  (trap '' INT QUIT HUP TERM
   exec "${fhs_command[@]}" env --default-signal=INT,QUIT,HUP,TERM bash ./scripts/nix-session.sh "$@") 0<&0 &
  local pid=$! status
  trap 'touch "$stop_request"' INT HUP TERM
  while :; do
    wait "$pid" && status=0 || status=$?
    kill -0 "$pid" 2>/dev/null || break
  done
  trap - INT HUP TERM
  rm -f "$stop_request"
  return "$status"
}
# Keep this supervisor alive to hold the lock: bubblewrap may close extra FDs.
# It never owns or signals game processes.
case "${1:-}" in
  --shell) shift; "${fhs_command[@]}" bash "$@";;
  --update) shift; "${fhs_command[@]}" ./scripts/update.sh "$@";;
  # In the namespace, whose root owns the prefix files, so a copy keeps owners.
  --set-prefix) "${fhs_command[@]}" python3 ./scripts/nix-prefix.py "$2";;
  --installer) shift; session --installer "$@";;
  *)
    if [ $# -gt 0 ]; then
      "${fhs_command[@]}" "$@"
    else
      session
    fi;;
esac
