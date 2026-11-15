#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
if [ -n "${DARLING_WORKSPACE:-}" ] && ! python3 scripts/nix-state.py check; then
  echo 'Rootless runtime readiness is missing or incompatible; run darling-lol --installer in a terminal.' >&2
  exit 1
fi
export PATH="${DARLING_RUNNER_DIR:+$DARLING_RUNNER_DIR:}$repo/usr-local/bin:/usr/local/bin:$PATH"
region=${1:-euw}
case "$region" in lan) region=la1;; las) region=la2;; oce) region=oc1;; esac
case "$region" in euw|eune|na|br|kr|jp|la1|la2|oc1|ru|tr) ;; *) echo 'Use a Riot installer region code' >&2; exit 1;; esac
python3 scripts/configure.py >/dev/null

# The prefix the installed client must appear in. runtime-paths.sh honors
# DPREFIX and otherwise uses the prefix recorded by `configure.py --prefix`,
# the same view launch-shortcut.sh uses when it decides to run this script.
if ! . build/runtime/tools/runtime-paths.sh >/dev/null || [ -z "${prefix_dir:-}" ]; then
  echo 'Could not resolve the Darling prefix; not installing the Riot Client.' >&2
  exit 1
fi
prefix=$prefix_dir
client_app="$prefix/Users/Shared/Riot Games/Riot Client.app"

client_installed() {
  [ -d "$client_app/Contents/MacOS" ]
}

# A crashed installer attempt leaves a partial client bundle (some Frameworks
# subdirectories, but no Contents/MacOS) and its 'Riot Client' metadata
# database behind. Remove both so the next attempt starts clean. A complete
# installation is never touched: its 'Riot Client' metadata is the release
# record riot-client-pin.py reads. The game metadata is always kept.
clean_partial_install() {
  if client_installed; then return 0; fi
  if [ -e "$client_app" ] || [ -L "$client_app" ]; then
    rm -rf "$client_app"
    echo "Removed partial $client_app"
  fi
  if [ -d "$prefix/Users/Shared/Riot Games/Metadata/Riot Client" ]; then
    rm -rf "$prefix/Users/Shared/Riot Games/Metadata/Riot Client"
    echo "Removed partial Riot Client metadata"
  fi
  # The runner links the installed client into Applications; remove the link
  # only when it no longer resolves to a complete installation.
  launcher_link="$prefix/Applications/Riot Client.app"
  if [ -L "$launcher_link" ] && [ ! -d "$launcher_link/Contents/MacOS" ]; then
    rm -f "$launcher_link"
    echo "Removed dangling $launcher_link"
  fi
}

# Surface the long installation and its outcome through the desktop
# notification service when it is reachable; menu launches have no terminal,
# so without this the only failure feedback would be this log.
notify() {
  command -v notify-send >/dev/null 2>&1 || return 0
  DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-"unix:path=/run/user/$(id -u)/bus"}
  export DBUS_SESSION_BUS_ADDRESS
  notify-send -a 'Darling-LoL' "$@" 2>/dev/null || true
}

# One install at a time. A second menu click during a slow install would
# otherwise reach clean_partial_install and delete the bundle the first run is
# still downloading. The lock is a directory (mkdir is atomic) holding the
# owner's pid; a lock whose owner is gone is stale and is taken over.
lock_dir="$repo/build/installer.lock"
lock_held=0
release_lock() {
  if [ "$lock_held" = 1 ]; then
    lock_held=0
    rm -rf "$lock_dir"
  fi
}
acquire_lock() {
  mkdir -p "$repo/build"
  tries=0
  while ! mkdir "$lock_dir" 2>/dev/null; do
    owner=$(cat "$lock_dir/pid" 2>/dev/null) || owner=
    case "$owner" in ''|*[!0-9]*) owner= ;; esac
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
      echo "A Riot Client install is already running (pid $owner); not starting another one." >&2
      notify 'Riot Client install already running' 'Wait for it to finish.'
      exit 1
    fi
    # No live owner. An owner that just created the directory may not have
    # written its pid yet, so give it a moment before calling the lock stale.
    tries=$((tries + 1))
    if [ -z "$owner" ] && [ "$tries" -le 2 ]; then sleep 1; continue; fi
    if [ "$tries" -gt 4 ]; then
      echo "Could not take the install lock $lock_dir; not installing." >&2
      exit 1
    fi
    echo "Removing the stale install lock $lock_dir${owner:+ (pid $owner is gone)}" >&2
    rm -rf "$lock_dir"
  done
  lock_held=1
  echo "$$" > "$lock_dir/pid"
}
# An interrupt stops the installer at once: no retry, and nothing more is
# removed. The shell runs these traps when the foreground run.sh returns.
interrupted() {
  echo 'Riot Client install interrupted; not retrying.' >&2
  exit "$1"
}
trap release_lock EXIT
trap 'interrupted 129' HUP
trap 'interrupted 130' INT
trap 'interrupted 143' TERM
acquire_lock

bash build/runtime/tools/fetch-installer.sh "$region" build/installer
unzip -q -o "build/installer/live.$region.zip" -d build/installer
RIOT_LAUNCHER_HOST="$repo/build/installer/Install League of Legends $region.app/Contents/MacOS/RiotClientServices"
RCLOL_REGION=$(printf '%s' "$region" | tr '[:lower:]' '[:upper:]')
export RIOT_LAUNCHER_HOST RCLOL_REGION

# The installer exits after installing the Riot Client; its own hand-off to the
# client fails under Darling, so start the installed client here. The
# installer's native UI has also crashed mid-install under Darling
# (LOLSIGFAIL, sig 11), leaving the partial state cleaned above, so retry a
# couple of times before giving up. A failed attempt leaves no runtime behind:
# the session supervisor shuts it down when the guest tree dies.
clean_partial_install
attempt=1
max_attempts=3
while :; do
  status=0
  sh scripts/run.sh || status=$?
  # run.sh ended by an interrupt (Ctrl-C reaches the whole foreground group,
  # and the session supervisor reports 128 + signal). A crash (sig 11) is not
  # an interrupt and stays eligible for a retry.
  case "$status" in 129|130|143) interrupted "$status" ;; esac
  client_installed && break
  [ "$attempt" -ge "$max_attempts" ] && break
  attempt=$((attempt + 1))
  clean_partial_install
  echo "The installer did not install the Riot Client; retrying ($attempt of $max_attempts)" >&2
  notify 'Riot Client install failed' "Retrying (attempt $attempt of $max_attempts)."
done
unset RIOT_LAUNCHER_HOST

if ! client_installed; then
  notify 'Riot Client install failed' 'See build/desktop-launch.log for details.'
  echo "The installer exited without installing the Riot Client after $attempt attempt(s). Its logs are in" >&2
  echo "$prefix/Users/<guest user>/Library/Logs/Riot Games/Install League of Legends $region/Launcher" >&2
  exit 1
fi

notify 'Riot Client installed' 'Starting it.'
# exec keeps this pid for the whole client session, so give the lock back now.
release_lock
trap - EXIT HUP INT TERM
exec sh scripts/run.sh
