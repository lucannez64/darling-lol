#!/bin/sh
# Removes every trace of the Riot software from the Darling prefix so
# scripts/installer.sh can do a fresh install. The Darling runtime itself
# (overlays in RuntimeLibraries/RuntimeFrameworks, darling-system.prop, the
# certificate store) and the Mesa shader cache are kept.
set -eu
umask 077
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"

usage() {
	cat <<'EOF' >&2
Usage: sh scripts/uninstall.sh [--with-game] [--all] [--yes]

Removes the Riot Client and its data from the Darling prefix. By default the
League of Legends game is removed too, because a fresh client install does not
reuse it.

  --with-game  Keep Applications/League of Legends.app and its product
               metadata; remove only the client and shared client data
  --all        Remove the whole prefix (runtime, overlays, certificates,
               settings, clients and the game)
  --yes        Do not confirm --all
EOF
	exit 1
}

with_game=0
whole_prefix=0
confirmed=0
for arg in "$@"; do
	case "$arg" in
	--with-game) with_game=1 ;;
	--all) whole_prefix=1 ;;
	--yes) confirmed=1 ;;
	-h | --help) usage ;;
	*) usage ;;
	esac
done

python3 scripts/configure.py >/dev/null
. build/runtime/tools/runtime-paths.sh
prefix=$prefix_dir
guest=$guest_home

case "$prefix" in
"/") echo 'Refusing to operate on the filesystem root.' >&2; exit 1 ;;
esac

# Read-only, fail-closed maintenance preflight: refuses to continue while the
# server marker is live or a client process still maps this prefix. Only the
# workspace session runs it, like install-runtime.sh does: from a plain host
# terminal the uid_map comparison cannot exclude host processes such as the
# unreadable systemd --user, and the check would fail on its own host.
if [ -n "${DARLING_WORKSPACE:-}" ]; then
	python3 scripts/nix-idle.py
fi

# End launchd/darlingserver so nothing holds the files being removed. Killing
# launchd ends the PID namespace, so clients and crash handlers go with it;
# stop-darling-runtime.py validates the marker identity before signalling.
if [ -f build/runtime/tools/stop-darling-runtime.py ]; then
	python3 build/runtime/tools/stop-darling-runtime.py
else
	echo 'build/runtime/tools/stop-darling-runtime.py is missing; make sure the runtime is stopped (darling shutdown) before removing files.' >&2
fi

if [ "$whole_prefix" = 1 ]; then
	if [ "$confirmed" != 1 ]; then
		printf 'This removes the WHOLE prefix %s: the Darling runtime, its overlays and\ncertificates, both clients and the game.\nType the prefix path to confirm: ' "$prefix"
		read -r reply
		[ "$reply" = "$prefix" ] || {
			echo 'Aborted.' >&2
			exit 1
		}
	fi
	rm -rf "$prefix"
	echo "Removed $prefix"
	echo 'Recreate it with: sh scripts/install-runtime.sh'
	exit 0
fi

# Riot logs can contain private session details. Keep a copy under build/
# (gitignored, mode 0600) so a failed reinstall can still be debugged.
if [ -d "$guest/Library/Logs" ]; then
	backup=$repo/build/uninstall-log-backups/$(date +%Y%m%d-%H%M%S)
	mkdir -p "$backup"
	chmod 700 "$backup" "$repo/build/uninstall-log-backups"
	for item in 'Riot Games' 'Riot Client'; do
		if [ -e "$guest/Library/Logs/$item" ]; then
			cp -a "$guest/Library/Logs/$item" "$backup/"
		fi
	done
	if [ "$(ls -A "$backup" 2>/dev/null | head -1)" != '' ]; then
		tar czf "$backup.tar.gz" -C "$backup" .
		rm -rf "$backup"
		chmod 600 "$backup.tar.gz"
		echo "Saved Riot logs in $backup.tar.gz"
	else
		rm -rf "$backup"
	fi
fi

removed=0
remove() {
	if [ -e "$1" ] || [ -L "$1" ]; then
		rm -rf "$1"
		echo "Removed $1"
		removed=1
	fi
}

# Shared install root: the client and its Metadata live here next to the game
# product settings. --with-game keeps the game app and its metadata so the
# kept game stays installable through the fresh client.
# Bundle display names can be localized (observed: a 'Cliente Riot.app' link
# to the client), so remove the expected names and any link that targets them.
shared=$prefix/Users/Shared/Riot\ Games
if [ "$with_game" = 1 ]; then
	if [ -d "$shared" ]; then
		find "$shared" -mindepth 1 -maxdepth 1 ! -path "$shared/Metadata" -exec rm -rf {} +
		if [ -d "$shared/Metadata" ]; then
			find "$shared/Metadata" -mindepth 1 -maxdepth 1 ! -path "$shared/Metadata/league_of_legends.live" -exec rm -rf {} +
		fi
		echo "Removed everything under $shared except the game metadata"
		removed=1
	fi
else
	remove "$shared"
	remove "$prefix/Applications/League of Legends.app"
fi
remove "$prefix/Applications/Riot Client.app"
if [ -d "$prefix/Applications" ]; then
	while IFS= read -r link; do
		rm -f "$link"
		echo "Removed $link"
		removed=1
	done <<EOF
$(find "$prefix/Applications" -maxdepth 1 -type l \( -lname '*Riot Client.app' -o -lname '*League of Legends.app' \) 2>/dev/null)
EOF
fi

remove "$guest/Library/Application Support/Riot Games"
remove "$guest/Library/Application Support/Riot Client"
remove "$guest/Library/Application Support/riot-client-ux"
remove "$guest/Library/Logs/Riot Games"
remove "$guest/Library/Logs/Riot Client"
# rm -f stays silent on unmatched globs, so these are safe when nothing exists.
rm -rf $guest/Library/Caches/com.riotgames.* 2>/dev/null || true
rm -f $guest/Library/Preferences/com.riotgames.*.plist 2>/dev/null || true

if [ "$removed" = 1 ]; then
	echo 'Riot software removed. The Darling runtime and the shader cache were kept.'
else
	echo 'Nothing of the Riot software was found in the prefix.'
fi
region=$(python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts')
from common import configuration
print(configuration()['region'].lower())
PY
)
echo "Fresh install: sh scripts/installer.sh $region"
