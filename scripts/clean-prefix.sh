#!/bin/sh
# Backs up and removes local Riot/League logs, machine.cfg and client session
# data/caches from the Darling prefix, and regenerates the prefix's synthetic
# platform identity (darling-platform.plist / darling-smbios.conf).
# Keeps the Darling runtime, Riot Client, League of Legends game files and
# Mesa shader cache intact. Nothing is stopped or modified before every
# refusal and the confirmation have passed.
set -eu
umask 077
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"

usage() {
	cat <<'EOF' >&2
Usage: sh scripts/clean-prefix.sh [--keep-identity] [--backup-dir DIR] [--yes]

Cleans local Riot/League logs, machine.cfg, client data/caches, and refreshes
the prefix's synthetic platform identity while keeping the Darling runtime,
Riot Client and League of Legends binaries installed. Removed logs, config
and previous identity files are archived (mode 0600) before deletion.

  --keep-identity   Keep darling-platform.plist and darling-smbios.conf unchanged
  --backup-dir DIR  Directory for timestamped backup archives
                    (default: build/prefix-cleanup-backups)
  --yes             Do not ask for confirmation (required without a terminal)
EOF
	exit 1
}

keep_identity=0
backup_dir=$repo/build/prefix-cleanup-backups
confirmed=0
while [ "$#" -gt 0 ]; do
	case "$1" in
	--keep-identity) keep_identity=1; shift ;;
	--backup-dir)
		[ "$#" -ge 2 ] && [ -n "$2" ] || usage
		backup_dir=$2
		shift 2
		;;
	--backup-dir=*)
		backup_dir=${1#--backup-dir=}
		[ -n "$backup_dir" ] || usage
		shift
		;;
	--yes) confirmed=1; shift ;;
	-h | --help) usage ;;
	*) usage ;;
	esac
done

python3 scripts/configure.py >/dev/null
. build/runtime/tools/runtime-paths.sh
prefix=$prefix_dir
guest=$guest_home

refuse() {
	echo "$1 Nothing was changed." >&2
	exit 1
}

[ -n "$prefix" ] || refuse 'Refusing to operate on the filesystem root.'
[ -d "$prefix" ] || refuse "$prefix does not exist."
resolved=$(CDPATH= cd -P -- "$prefix" && pwd -P)
[ "$resolved" != / ] || refuse 'Refusing to operate on the filesystem root.'
if [ -n "${HOME:-}" ] && [ -d "$HOME" ]; then
	[ "$resolved" != "$(CDPATH= cd -P -- "$HOME" && pwd -P)" ] || refuse 'Refusing to operate on the home directory.'
fi
if [ ! -d "$prefix/Volumes/SystemRoot" ] && [ ! -f "$prefix/private/etc/darling-system.prop" ]; then
	refuse "$prefix is not a Darling prefix (no Volumes/SystemRoot and no private/etc/darling-system.prop)."
fi

# Refuse while a Riot Client or League process is running; CLEAN_PREFIX_PROC_ROOT exists for tests.
python3 - <<'PY'
import os
from pathlib import Path
for entry in Path(os.environ.get('CLEAN_PREFIX_PROC_ROOT') or '/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        command_line = (entry / 'cmdline').read_bytes()
    except OSError:
        continue
    if b'/Riot Client.app/' in command_line or b'/League of Legends.app/' in command_line:
        raise SystemExit('A Riot Client or League process is running; exit the game and both clients, then run this again. Nothing was changed.')
PY

if [ -n "${DARLING_WORKSPACE:-}" ]; then
	python3 scripts/nix-idle.py
fi

if [ ! -f build/runtime/tools/stop-darling-runtime.py ]; then
	refuse 'build/runtime/tools/stop-darling-runtime.py is missing, so the runtime cannot be stopped. Close the game and both clients, run `darling shutdown`, and restore the checkout before running this again.'
fi

echo "Darling prefix: $prefix"
echo "Backup directory: $backup_dir"
cat <<'EOF'
Will be backed up and cleaned: Riot/League logs, machine.cfg, Riot Client
  local session data, client caches (com.riotgames.*) and stale runtime markers.
EOF
if [ "$keep_identity" = 1 ]; then
	echo 'Platform identity: kept unchanged (--keep-identity).'
else
	echo 'Platform identity: previous files backed up and synthetic identifiers regenerated.'
fi
echo 'Will be kept: the Darling runtime, Riot Client.app, League of Legends.app, Metadata and the shader cache.'
echo 'The Darling runtime is stopped first.'

if [ "$confirmed" != 1 ]; then
	if [ ! -t 0 ]; then
		refuse 'No terminal to ask for confirmation; pass --yes to proceed without asking.'
	fi
	printf 'Continue? [y/N] '
	read -r reply || reply=''
	case "$reply" in
	y | Y | yes | Yes | YES) ;;
	*) refuse 'Aborted.' ;;
	esac
fi

python3 build/runtime/tools/stop-darling-runtime.py

# Archive existing logs, local machine/session config and previous platform
# identity to a mode-0600 tarball before deleting anything. Symlinks are never
# followed outside the prefix.
archive_path=$(python3 - "$prefix" "$guest" "$backup_dir" "$keep_identity" <<'PY'
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

prefix = Path(sys.argv[1]).resolve()
guest = Path(sys.argv[2])
backup_dir = Path(sys.argv[3])
keep_identity = sys.argv[4] == '1'

targets = [
    guest / 'Library/Logs/Riot Games',
    guest / 'Library/Logs/Riot Client',
    prefix / 'Applications/League of Legends.app/Contents/LoL/Logs',
    prefix / 'Users/Shared/Riot Games/machine.cfg',
    guest / 'Library/Application Support/Riot Games/machine.cfg',
    guest / 'Library/Application Support/Riot Games/Riot Client/Config/machine.cfg',
    guest / 'Library/Application Support/Riot Games/Riot Client/Data',
]
if not keep_identity:
    targets.extend([
        prefix / 'private/etc/darling-platform.plist',
        prefix / 'private/etc/darling-smbios.conf',
    ])

def safe_within(path):
    try:
        return path.resolve().is_relative_to(prefix)
    except OSError:
        return False

to_archive = []
for target in targets:
    if not target.exists() or target.is_symlink():
        continue
    if not safe_within(target):
        continue
    to_archive.append(target)

if not to_archive:
    sys.exit(0)

backup_dir.mkdir(parents=True, exist_ok=True)
os.chmod(backup_dir, 0o700)
stamp = time.strftime('%Y%m%d-%H%M%S')
dest = backup_dir / f'prefix-cleanup-{stamp}.tar.gz'
suffix = 1
while dest.exists():
    dest = backup_dir / f'prefix-cleanup-{stamp}-{suffix}.tar.gz'
    suffix += 1

fd, tmp_name = tempfile.mkstemp(prefix='.prefix-cleanup-', suffix='.tar.gz', dir=backup_dir)
os.close(fd)
tmp_path = Path(tmp_name)
added = 0
try:
    with tarfile.open(tmp_path, 'w:gz') as tar:
        for item in to_archive:
            if item.is_file():
                rel = item.resolve().relative_to(prefix)
                tar.add(item, arcname=str(rel), recursive=False)
                added += 1
            elif item.is_dir():
                for root, dirs, files in os.walk(item, followlinks=False):
                    root_path = Path(root)
                    if not safe_within(root_path):
                        dirs[:] = []
                        continue
                    rel_root = root_path.resolve().relative_to(prefix)
                    tar.add(root_path, arcname=str(rel_root), recursive=False)
                    added += 1
                    dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
                    for name in files:
                        child = root_path / name
                        if child.is_symlink() or not safe_within(child):
                            continue
                        tar.add(child, arcname=str(child.resolve().relative_to(prefix)), recursive=False)
                        added += 1
    if added:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, dest)
        print(dest)
    else:
        tmp_path.unlink(missing_ok=True)
except Exception:
    tmp_path.unlink(missing_ok=True)
    raise
PY
)

if [ -n "$archive_path" ]; then
	echo "Saved backup in $archive_path"
fi

cleaned=0
remove() {
	if [ -e "$1" ] || [ -L "$1" ]; then
		rm -rf -- "$1"
		echo "Removed $1"
		cleaned=1
	fi
}

remove "$guest/Library/Logs/Riot Games"
remove "$guest/Library/Logs/Riot Client"
remove "$prefix/Applications/League of Legends.app/Contents/LoL/Logs"
remove "$prefix/Users/Shared/Riot Games/machine.cfg"
remove "$guest/Library/Application Support/Riot Games/machine.cfg"
remove "$guest/Library/Application Support/Riot Games/Riot Client/Config/machine.cfg"
remove "$guest/Library/Application Support/Riot Games/Riot Client/Data"
for item in "$guest/Library/Caches"/com.riotgames.* "$prefix"/.init.pid.stale-*; do
	remove "$item"
done

if [ "$keep_identity" != 1 ]; then
	if [ -n "${CLEAN_PREFIX_RUNTIME_ROOT:-}" ]; then
		python3 tools/configure-darling-platform.py --prefix "$prefix" \
			--runtime-root "$CLEAN_PREFIX_RUNTIME_ROOT" --regenerate --write
	else
		python3 tools/configure-darling-platform.py --prefix "$prefix" \
			--regenerate --write
	fi
	cleaned=1
fi

if [ "$cleaned" = 1 ]; then
	echo 'Prefix cleanup complete. The Darling runtime, Riot Client, League of Legends and shader cache were kept.'
else
	echo 'Prefix was already clean; nothing was changed.'
fi
