#!/bin/sh
# Host handler for x-scheme-handler/riotclient: hand the URL to the running guest.
#
# Google and other social sign-in finish in the host browser, which then opens
# riotclient://auth/... . Inside Darling, open(1) delivers that URL to the running
# Riot Client as the kAEGetURL Apple event macOS would send (LaunchServices and
# AppKit, "URL events for running apps"). This only forwards into a session that is
# already running: it never starts Darling, and it never logs the URL, which carries
# a sign-in token.
set -eu
runner_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$runner_dir/runtime-paths.sh"

url=${1:-}
case "$url" in
 riotclient://*) : ;;
 *) echo 'open-guest-url: only riotclient:// URLs are handled' >&2; exit 2 ;;
esac
# The guest rejects these too; refuse them before they reach Darling.
if [ "$(printf '%s' "$url" | wc -c)" -gt 4096 ]; then
 echo 'open-guest-url: URL longer than 4096 bytes' >&2
 exit 2
fi
# Count what is left instead of testing $(...): it would strip trailing newlines.
if [ "$(printf '%s' "$url" | LC_ALL=C tr -d '[:print:]' | wc -c)" -ne 0 ]; then
 echo 'open-guest-url: URL contains control or non-ASCII characters' >&2
 exit 2
fi

if [ "$(python3 "$runner_dir/session-supervisor.py" --prefix "$prefix_dir" --probe-runtime)" != running ]; then
 echo 'open-guest-url: Darling is not running; start the Riot Client and sign in again' >&2
 exit 3
fi
darling_bin=$(command -v darling) || { echo 'open-guest-url: darling loader not found in PATH' >&2; exit 1; }
if "$darling_bin" shell /usr/bin/open "$url" </dev/null >/dev/null 2>&1; then
 echo 'open-guest-url: delivered a riotclient URL to the guest'
else
 status=$?
 echo "open-guest-url: the guest did not accept the URL (open exited $status; is the Riot Client running?)" >&2
 exit 4
fi
