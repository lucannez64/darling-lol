#!/bin/sh
# Backs up and removes local Riot/League logs, machine.cfg, client profiles,
# crash reports, local config/caches and stale runtime markers from the Darling
# prefix, and regenerates the prefix's synthetic platform identity
# (darling-platform.plist / darling-smbios.conf) without leaving
# *.before-platform-change identity copies behind in private/etc.
# Keeps the Darling runtime, Riot Client.app, League of Legends.app, Metadata
# and Mesa shader cache intact. Nothing is stopped or modified before every
# refusal and the confirmation have passed.
set -eu
umask 077
caller_pwd=$(pwd)
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"

usage() {
	cat <<'EOF' >&2
Usage: sh scripts/clean-prefix.sh [--keep-identity] [--keep-game-settings] [--backup-dir DIR] [--yes]

Cleans local Riot/League logs, machine.cfg, Riot Client / Electron / CEF
profiles, unsent crash reports, local client config/caches and stale runtime
markers, and refreshes the prefix's synthetic platform identity while keeping
the Darling runtime, Riot Client and League of Legends binaries installed.
Removed logs, config, profiles and previous identity files are archived
(mode 0600) before deletion.

  --keep-identity       Keep darling-platform.plist and darling-smbios.conf unchanged
  --keep-game-settings  Keep League of Legends Config/game.cfg and Config/input.ini
  --backup-dir DIR      Directory for timestamped backup archives
                        (default: build/prefix-cleanup-backups)
  --yes                 Do not ask for confirmation (required without a terminal)
EOF
	exit 1
}

keep_identity=0
keep_game_settings=0
backup_dir=$repo/build/prefix-cleanup-backups
custom_backup_dir=0
confirmed=0
while [ "$#" -gt 0 ]; do
	case "$1" in
	--keep-identity) keep_identity=1; shift ;;
	--keep-game-settings) keep_game_settings=1; shift ;;
	--backup-dir)
		[ "$#" -ge 2 ] && [ -n "$2" ] || usage
		backup_dir=$2
		custom_backup_dir=1
		shift 2
		;;
	--backup-dir=*)
		backup_dir=${1#--backup-dir=}
		[ -n "$backup_dir" ] || usage
		custom_backup_dir=1
		shift
		;;
	--yes) confirmed=1; shift ;;
	-h | --help) usage ;;
	*) usage ;;
	esac
done

if [ "$custom_backup_dir" = 1 ]; then
	case "$backup_dir" in
	/* | '~' | '~/'*) ;;
	*) backup_dir=$caller_pwd/$backup_dir ;;
	esac
fi

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

# Validate the backup destination before stopping the runtime: it must stay
# outside the prefix being cleaned and be writable (or creatable).
python3 - "$resolved" "$backup_dir" <<'PY'
import os
import sys
from pathlib import Path

prefix = Path(sys.argv[1]).resolve()
backup = Path(sys.argv[2]).expanduser()
try:
    backup_resolved = backup.resolve()
except OSError as exc:
    raise SystemExit(f'Cannot resolve backup directory {backup}: {exc}. Nothing was changed.')

if backup_resolved == prefix or backup_resolved.is_relative_to(prefix):
    raise SystemExit(f'Refusing backup directory inside the Darling prefix: {backup}. Nothing was changed.')

if backup.exists():
    if not backup.is_dir() or not os.access(backup, os.W_OK | os.X_OK):
        raise SystemExit(f'Backup directory is not writable: {backup}. Nothing was changed.')
else:
    ancestor = backup.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise SystemExit(f'Cannot create backup directory {backup}: parent {ancestor} is not writable. Nothing was changed.')
PY

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
Will be backed up and cleaned: Riot/League logs, machine.cfg, Riot Games /
  Riot Client / riot-client-ux / CEF Application Support profiles and crash
  reports, com.riotgames.*.plist preferences, and League client Config files.
Will be cleaned without backup: client caches (com.riotgames.*), League
  Saved/webcache, and stale runtime markers.
EOF
if [ "$keep_game_settings" = 1 ]; then
	echo 'Game settings: Config/game.cfg and Config/input.ini kept (--keep-game-settings).'
else
	echo 'Game settings: Config/game.cfg and Config/input.ini backed up and removed.'
fi
if [ "$keep_identity" = 1 ]; then
	echo 'Platform identity: kept unchanged (--keep-identity).'
else
	echo 'Platform identity: previous files backed up, *.before-platform-change removed, and synthetic identifiers regenerated.'
fi
echo 'Will be kept: the Darling runtime, Riot Client.app, League of Legends.app game files, Metadata and the shader cache.'
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

# Archive existing logs, local client profiles/config/crashes and previous
# platform identity to a mode-0600 tarball before deleting anything. Every
# target is checked so neither archival nor deletion follows symlinks outside
# the prefix.
cleaned_count=$(python3 - "$prefix" "$guest" "$backup_dir" "$keep_identity" "$keep_game_settings" <<'PY'
import errno
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

prefix = Path(sys.argv[1]).resolve()
guest = Path(sys.argv[2])
backup_dir = Path(sys.argv[3]).expanduser()
keep_identity = sys.argv[4] == '1'
keep_game_settings = sys.argv[5] == '1'

def safe_within(path):
    try:
        return path.resolve().is_relative_to(prefix)
    except OSError:
        return False

def parent_within_prefix(path):
    try:
        parent = path.parent.resolve()
        return parent == prefix or parent.is_relative_to(prefix)
    except OSError:
        return False

def ensure_private_dir(path):
    """Create missing directories with mode 0700 without modifying existing ones."""
    missing = []
    cur = path
    while not cur.exists() and cur != cur.parent:
        missing.append(cur)
        cur = cur.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)

lol_root = prefix / 'Applications/League of Legends.app/Contents/LoL'
lol_config = lol_root / 'Config'
lol_config_files = [
    lol_config / 'LeagueClientSettings.yaml',
    lol_config / 'LCUAccountPreferences.yaml',
    lol_config / 'PersistedSettings.json',
]
if not keep_game_settings:
    lol_config_files.extend([
        lol_config / 'game.cfg',
        lol_config / 'input.ini',
    ])

prefs_files = []
prefs_dir = guest / 'Library/Preferences'
if os.path.lexists(prefs_dir) and not prefs_dir.is_symlink() and parent_within_prefix(prefs_dir) and safe_within(prefs_dir):
    for entry in sorted(prefs_dir.iterdir()):
        if entry.name.startswith('com.riotgames.') and entry.name.endswith('.plist'):
            prefs_files.append(entry)

backed_up_targets = [
    guest / 'Library/Logs/Riot Games',
    guest / 'Library/Logs/Riot Client',
    lol_root / 'Logs',
    prefix / 'Users/Shared/Riot Games/machine.cfg',
    guest / 'Library/Application Support/Riot Games',
    guest / 'Library/Application Support/Riot Client',
    guest / 'Library/Application Support/riot-client-ux',
    guest / 'Library/Application Support/CEF',
    *prefs_files,
    *lol_config_files,
]
identity_backups = [
    prefix / 'private/etc/darling-platform.plist.before-platform-change',
    prefix / 'private/etc/darling-smbios.conf.before-platform-change',
]

archive_targets = list(backed_up_targets)
if not keep_identity:
    archive_targets.extend([
        prefix / 'private/etc/darling-platform.plist',
        prefix / 'private/etc/darling-smbios.conf',
        *identity_backups,
    ])

to_archive = []
for target in archive_targets:
    if not os.path.lexists(target) or target.is_symlink():
        continue
    if not parent_within_prefix(target) or not safe_within(target):
        continue
    to_archive.append(target)

if to_archive:
    ensure_private_dir(backup_dir)
    stamp = time.strftime('%Y%m%d-%H%M%S')
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
            suffix = 0
            while True:
                name = f'prefix-cleanup-{stamp}.tar.gz' if suffix == 0 else f'prefix-cleanup-{stamp}-{suffix}.tar.gz'
                dest = backup_dir / name
                try:
                    os.link(tmp_path, dest)
                    tmp_path.unlink(missing_ok=True)
                    break
                except FileExistsError:
                    suffix += 1
                except OSError as exc:
                    if exc.errno not in (errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS):
                        raise
                    try:
                        out_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        suffix += 1
                        continue
                    try:
                        with os.fdopen(out_fd, 'wb') as out_f, tmp_path.open('rb') as in_f:
                            shutil.copyfileobj(in_f, out_f)
                            out_f.flush()
                            os.fsync(out_f.fileno())
                    except Exception:
                        dest.unlink(missing_ok=True)
                        raise
                    tmp_path.unlink(missing_ok=True)
                    break
            print(f'Saved backup in {dest}', file=sys.stderr)
        else:
            tmp_path.unlink(missing_ok=True)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

remove_targets = [
    *backed_up_targets,
    lol_root / 'Saved/webcache',
]
if not keep_identity:
    remove_targets.extend(identity_backups)

caches_dir = guest / 'Library/Caches'
if os.path.lexists(caches_dir) and not caches_dir.is_symlink() and parent_within_prefix(caches_dir) and safe_within(caches_dir):
    for entry in sorted(caches_dir.iterdir()):
        if entry.name.startswith('com.riotgames.'):
            remove_targets.append(entry)

for entry in sorted(prefix.iterdir()):
    if entry.name.startswith('.init.pid.stale-'):
        remove_targets.append(entry)

removed = 0
for target in remove_targets:
    if not os.path.lexists(target):
        continue
    # Never delete through a parent directory symlink pointing outside the prefix.
    if not parent_within_prefix(target):
        print(f'Skipped (outside prefix): {target}', file=sys.stderr)
        continue
    if target.is_symlink():
        target.unlink()
    elif not safe_within(target):
        print(f'Skipped (outside prefix): {target}', file=sys.stderr)
        continue
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    print(f'Removed {target}', file=sys.stderr)
    removed += 1

print(removed)
PY
)

cleaned=0
if [ "${cleaned_count:-0}" -gt 0 ]; then
	cleaned=1
fi

if [ "$keep_identity" != 1 ]; then
	if [ -n "${CLEAN_PREFIX_RUNTIME_ROOT:-}" ]; then
		python3 tools/configure-darling-platform.py --prefix "$prefix" \
			--runtime-root "$CLEAN_PREFIX_RUNTIME_ROOT" --regenerate --no-backup --write
	else
		python3 tools/configure-darling-platform.py --prefix "$prefix" \
			--regenerate --no-backup --write
	fi
	cleaned=1
fi

if [ "$cleaned" = 1 ]; then
	echo 'Prefix cleanup complete. The Darling runtime, Riot Client, League of Legends and shader cache were kept.'
else
	echo 'Prefix was already clean; nothing was changed.'
fi
