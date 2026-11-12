#!/bin/sh
# Desktop entry point for riotclient:// links (darling-url-handler.desktop).
# Browsers start it without a terminal, so keep the outcome in build/open-url.log.
# tools/open-guest-url.sh never writes the URL itself: it carries a sign-in token.
set -eu
umask 077
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
export PATH="$repo/usr-local/bin:/usr/local/bin:$PATH"
mkdir -p build
log="$repo/build/open-url.log"
touch "$log"
chmod 600 "$log"
exec >>"$log" 2>&1
printf '%s ' "$(date '+%F %T')"
exec sh build/runtime/tools/open-guest-url.sh "$@"
