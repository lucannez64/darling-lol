#!/bin/sh
# Removes every trace of the Riot software from the Darling prefix so
# scripts/installer.sh can do a fresh install. The Darling runtime itself
# (overlays in RuntimeLibraries/RuntimeFrameworks, darling-system.prop, the
# certificate store) and the Mesa shader cache are kept.
# Nothing is stopped or deleted before every refusal and the confirmation
# have passed.
set -eu
umask 077
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"

usage() {
	cat <<'EOF' >&2
Usage: sh scripts/uninstall.sh [--keep-game] [--all] [--yes]

Removes the Riot Client and its data from the Darling prefix. By default the
League of Legends game is removed too, because a fresh client install does not
reuse it. The script lists what it removes and asks before doing anything.

  --keep-game  Keep Applications/League of Legends.app and its product
               metadata; remove only the client and shared client data
  --all        Remove the whole prefix (runtime, overlays, certificates,
               settings, clients and the game)
  --yes        Do not ask for confirmation (required without a terminal)
EOF
	exit 1
}

keep_game=0
whole_prefix=0
confirmed=0
for arg in "$@"; do
	case "$arg" in
	--keep-game) keep_game=1 ;;
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

refuse() {
	echo "$1 Nothing was removed." >&2
	exit 1
}

# runtime-paths.sh strips one trailing slash, so the filesystem root arrives
# here as an empty string. Compare resolved paths so a link to / or to the
# home directory is refused as well.
[ -n "$prefix" ] || refuse 'Refusing to operate on the filesystem root.'
[ -d "$prefix" ] || refuse "$prefix does not exist."
resolved=$(CDPATH= cd -P -- "$prefix" && pwd -P)
[ "$resolved" != / ] || refuse 'Refusing to operate on the filesystem root.'
if [ -n "${HOME:-}" ] && [ -d "$HOME" ]; then
	[ "$resolved" != "$(CDPATH= cd -P -- "$HOME" && pwd -P)" ] || refuse 'Refusing to operate on the home directory.'
fi
# A Darling prefix has the mount point Darling creates when it starts one
# (the scripts/nix-prefix.py check) or the system properties
# scripts/install-runtime.sh installs. Anything else is not ours to delete.
if [ ! -d "$prefix/Volumes/SystemRoot" ] && [ ! -f "$prefix/private/etc/darling-system.prop" ]; then
	refuse "$prefix is not a Darling prefix (no Volumes/SystemRoot and no private/etc/darling-system.prop)."
fi

# Stopping the runtime ends a running match, so refuse rather than end a
# session, as scripts/update.sh does. UNINSTALL_PROC_ROOT exists for the tests.
python3 - <<'PY'
import os
from pathlib import Path
for entry in Path(os.environ.get('UNINSTALL_PROC_ROOT') or '/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        command_line = (entry / 'cmdline').read_bytes()
    except OSError:
        continue
    if b'/Riot Client.app/' in command_line or b'/League of Legends.app/' in command_line:
        raise SystemExit('A Riot Client or League process is running; exit the game and both clients, then run this again. Nothing was removed.')
PY

# Read-only, fail-closed maintenance preflight: refuses to continue while the
# server marker is live or a client process still maps this prefix. Only the
# workspace session runs it, like install-runtime.sh does: from a plain host
# terminal the uid_map comparison cannot exclude host processes such as the
# unreadable systemd --user, and the check would fail on its own host.
if [ -n "${DARLING_WORKSPACE:-}" ]; then
	python3 scripts/nix-idle.py
fi

# Files are only removed once launchd/darlingserver are down, so without the
# helper that stops them there is nothing safe to do.
if [ ! -f build/runtime/tools/stop-darling-runtime.py ]; then
	refuse 'build/runtime/tools/stop-darling-runtime.py is missing, so the runtime cannot be stopped. Close the game and both clients, run `darling shutdown`, and restore the checkout before running this again.'
fi

echo "Darling prefix: $prefix"
if [ "$whole_prefix" = 1 ]; then
	cat <<'EOF'
Will be removed: the WHOLE prefix - the Darling runtime, its overlays and
  certificates, all settings, the Riot Client and League of Legends.
Will be kept: nothing inside the prefix.
EOF
elif [ "$keep_game" = 1 ]; then
	cat <<'EOF'
Will be removed: the Riot Client, its metadata, settings, logs (a copy is
  saved under build/uninstall-log-backups/) and caches.
Will be kept: League of Legends (Applications/League of Legends.app and its
  product metadata), the Darling runtime and the shader cache.
EOF
else
	cat <<'EOF'
Will be removed: the Riot Client and League of Legends (the whole game
  download), their metadata, settings, logs (a copy is saved under
  build/uninstall-log-backups/) and caches.
Will be kept: the Darling runtime and the shader cache. Pass --keep-game to
  keep the game too.
EOF
fi
echo 'The Darling runtime is stopped first.'

if [ "$confirmed" != 1 ]; then
	if [ ! -t 0 ]; then
		refuse 'No terminal to ask for confirmation; pass --yes to proceed without asking.'
	fi
	if [ "$whole_prefix" = 1 ]; then
		printf 'Type the prefix path to confirm: '
		read -r reply || reply=''
		[ "$reply" = "$prefix" ] || refuse 'Aborted.'
	else
		printf 'Continue? [y/N] '
		read -r reply || reply=''
		case "$reply" in
		y | Y | yes | Yes | YES) ;;
		*) refuse 'Aborted.' ;;
		esac
	fi
fi

# End launchd/darlingserver so nothing holds the files being removed. Killing
# launchd ends the PID namespace, so clients and crash handlers go with it;
# stop-darling-runtime.py validates the marker identity before signalling.
python3 build/runtime/tools/stop-darling-runtime.py

if [ "$whole_prefix" = 1 ]; then
	rm -rf -- "$prefix"
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
			cp -a -- "$guest/Library/Logs/$item" "$backup/"
		fi
	done
	if [ "$(ls -A "$backup" 2>/dev/null | head -1)" != '' ]; then
		tar czf "$backup.tar.gz" -C "$backup" .
		rm -rf -- "$backup"
		chmod 600 "$backup.tar.gz"
		echo "Saved Riot logs in $backup.tar.gz"
	else
		rm -rf -- "$backup"
	fi
fi

# rm never follows a link given as its operand, so a link that leaves the
# prefix is removed and its target is left alone.
removed=0
remove() {
	if [ -e "$1" ] || [ -L "$1" ]; then
		rm -rf -- "$1"
		echo "Removed $1"
		removed=1
	fi
}

# Shared install root: the client and its Metadata live here next to the game
# product settings. --keep-game keeps the game app and its metadata so the
# kept game stays installable through the fresh client.
# An unmatched pattern stays literal and names nothing, which remove skips.
shared=$prefix/Users/Shared/Riot\ Games
if [ "$keep_game" = 1 ]; then
	if [ -d "$shared" ] && [ ! -L "$shared" ]; then
		for item in "$shared"/* "$shared"/.[!.]* "$shared"/..?*; do
			[ "$item" = "$shared/Metadata" ] || remove "$item"
		done
		if [ -d "$shared/Metadata" ] && [ ! -L "$shared/Metadata" ]; then
			for item in "$shared/Metadata"/* "$shared/Metadata"/.[!.]* "$shared/Metadata"/..?*; do
				[ "$item" = "$shared/Metadata/league_of_legends.live" ] || remove "$item"
			done
		fi
	fi
else
	remove "$shared"
	remove "$prefix/Applications/League of Legends.app"
fi
remove "$prefix/Applications/Riot Client.app"
# Bundle display names can be localized (observed: a 'Cliente Riot.app' link
# to the client), so remove any link that targets the expected names too.
for link in "$prefix/Applications"/*; do
	[ -L "$link" ] || continue
	target=$(readlink -- "$link") || continue
	case "$target" in
	*'Riot Client.app') remove "$link" ;;
	*'League of Legends.app') [ "$keep_game" = 1 ] || remove "$link" ;;
	esac
done

remove "$guest/Library/Application Support/Riot Games"
remove "$guest/Library/Application Support/Riot Client"
remove "$guest/Library/Application Support/riot-client-ux"
remove "$guest/Library/Logs/Riot Games"
remove "$guest/Library/Logs/Riot Client"
for item in "$guest/Library/Caches"/com.riotgames.* "$guest/Library/Preferences"/com.riotgames.*.plist; do
	remove "$item"
done

if [ "$removed" = 1 ]; then
	echo 'Riot software removed. The Darling runtime and the shader cache were kept.'
else
	echo 'Nothing of the Riot software was installed in the prefix; nothing was removed.'
fi
region=$(python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts')
from common import configuration
print(configuration()['region'].lower())
PY
)
echo "Fresh install: sh scripts/installer.sh $region"
