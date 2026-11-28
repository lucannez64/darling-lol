#!/bin/sh
# Ad-hoc sign a Darling runtime tree so the M2 loader gate can enforce.
#
# Usage:
#   sign-darling-runtime.sh [options] [DIR...]
#
#   --sdk             add /usr/local/libexec/darling (needs root)
#   --prefix          add the per-user prefix overlays (RuntimeLibraries,
#                     RuntimeFrameworks, usr/lib/system) under ~/.darling
#   --dry-run         list what would be signed
#   --force           re-sign files that already have a signature
#   --verify          verify after signing (default)
#   --no-verify       skip the post-signing dc-codesign verify
#   --no-thin         do not thin single-architecture x86_64 fat binaries
#   --trust           add signed images to the local trust store
#   --no-trust        do not update the trust store
#   --trust-store P   trust store path (implies --trust)
#   --allow-running   permit signing while darlingserver is running
#   --quiet           only print the summary
#   --no-cache        always run rcodesign instead of reusing signed copies
#
# With no DIR and no --sdk/--prefix, both defaults are used.
# Only SDK roots are trusted automatically; overlays require explicit --trust.
#
# Idempotent: files that already carry a CodeDirectory are skipped unless
# --force. Relocatable objects (MH_OBJECT) are skipped because they are not
# loadable images. A fat binary whose only slice is x86_64 is thinned in place
# (the guest target is thin x86-64) and then signed; multi-architecture fat
# binaries are skipped.
#
# Signed-copy cache: `cmake --install` puts the unsigned build output back over
# every binary that was signed in place, so each update used to re-sign the whole
# SDK. A signed copy of each binary is kept under its unsigned content hash (and
# identifier); when the same unsigned binary comes back it is restored from the
# cache instead of signed again, so only binaries that really changed run
# rcodesign. Cache: $DARLING_SIGN_CACHE, else /var/cache/darling-lol/signed for
# root and ~/.cache/darling-lol/signed otherwise. It is used only when owned by
# the caller and not writable by group or others, one directory per signed root,
# and pruned to the binaries the root still has.
#
# Exit: 0 all good, 1 some file failed, 2 usage/setup error.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DCC=${DCC:-$here/dc-codesign/dc-codesign}
DCT=${DCT:-$here/dc-trustcache.py}
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.cargo/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.cargo/bin/rcodesign"
fi
LIPO=${LIPO:-$(command -v llvm-lipo 2>/dev/null || command -v lipo 2>/dev/null || true)}

dry=0
force=0
verify=1
thin=1
trust=0
sdk_selected=0
no_trust=0
trust_store=""
allow_running=0
quiet=0
cache=1
roots=""
explicit=0

usage() {
	sed -n "2,/^$/p" "$0" | sed 's/^# \{0,1\}//'
	exit 2
}

while [ $# -gt 0 ]; do
	case "$1" in
	--sdk) roots="$roots /usr/local/libexec/darling"; explicit=1; sdk_selected=1 ;;
	--prefix)
		prefix_root=${DPREFIX:-${HOME}/.darling}
		for d in \
		    "$prefix_root"/Users/*/RuntimeLibraries \
		    "$prefix_root"/Users/*/RuntimeFrameworks \
		    "$prefix_root"/usr/lib \
		    "$prefix_root"/System/Library/Frameworks \
		    "$prefix_root"/Users/*/Library/Audio/Plug-Ins/Components \
		    "$prefix_root"/Users/*/libDarlingXShm.dylib \
		    "$prefix_root"/Users/*/libDarlingWayland.dylib; do
			{ [ -d "$d" ] || [ -f "$d" ]; } && roots="$roots $d"
		done
		explicit=1 ;;
	--dry-run) dry=1 ;;
	--force) force=1 ;;
	--verify) verify=1 ;;
	--no-verify) verify=0 ;;
	--no-thin) thin=0 ;;
	--trust) trust=1; no_trust=0 ;;
	--no-trust) trust=0; no_trust=1 ;;
	--trust-store) trust=1; no_trust=0; trust_store=$2; shift ;;
	--allow-running) allow_running=1 ;;
	--quiet) quiet=1 ;;
	--no-cache) cache=0 ;;
	-h|--help) usage ;;
	-*) echo "sign-darling-runtime.sh: unknown option $1" >&2; usage ;;
	*) roots="$roots $1"; explicit=1 ;;
	esac
	shift
done

if [ "$explicit" -eq 0 ]; then
	sdk_selected=1
	roots="/usr/local/libexec/darling"
	prefix_root=${DPREFIX:-${HOME}/.darling}
	for d in \
	    "$prefix_root"/Users/*/RuntimeLibraries \
	    "$prefix_root"/Users/*/RuntimeFrameworks \
	    "$prefix_root"/usr/lib \
	    "$prefix_root"/System/Library/Frameworks \
	    "$prefix_root"/Users/*/Library/Audio/Plug-Ins/Components \
	    "$prefix_root"/Users/*/libDarlingXShm.dylib \
	    "$prefix_root"/Users/*/libDarlingWayland.dylib; do
		{ [ -d "$d" ] || [ -f "$d" ]; } && roots="$roots $d"
	done
fi

if [ ! -f "$DCC" ] || [ ! -x "$DCC" ]; then
	echo "sign-darling-runtime.sh: dc-codesign not built at $DCC" >&2
	echo "  run: make -C $here/dc-codesign" >&2
	exit 2
fi
if [ -z "$RCODESIGN" ] || [ ! -x "$RCODESIGN" ]; then
	echo "sign-darling-runtime.sh: rcodesign not found; run tools/install-rcodesign.sh" >&2
	exit 2
fi
if { [ "$trust" -eq 1 ] || { [ "$sdk_selected" -eq 1 ] && [ "$no_trust" -eq 0 ]; }; } &&
    { [ -z "$DCT" ] || [ ! -f "$DCT" ]; }; then
	echo "sign-darling-runtime.sh: trust-cache tool not found at $DCT" >&2
	exit 2
fi

if [ "$allow_running" -eq 0 ] && [ "$dry" -eq 0 ] && pgrep -x darlingserver >/dev/null 2>&1; then
	echo "sign-darling-runtime.sh: darlingserver is running; run 'darling shutdown' first" >&2
	echo "  (or pass --allow-running if you know the tree is not in use)" >&2
	exit 2
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
trust_entries=$tmp/trust-entries
: > "$trust_entries"

# --- signed-copy cache ------------------------------------------------------
cache_root=""
# --force asks for a fresh signature, so it never restores a cached one.
if [ "$cache" -eq 1 ] && [ "$dry" -eq 0 ] && [ "$force" -eq 0 ]; then
	cache_root=${DARLING_SIGN_CACHE:-}
	if [ -z "$cache_root" ]; then
		if [ "$(id -u)" -eq 0 ]; then
			cache_root=/var/cache/darling-lol/signed
		else
			cache_root=${XDG_CACHE_HOME:-${HOME:-/nonexistent}/.cache}/darling-lol/signed
		fi
	fi
	# A copy restored from here is installed and trusted, so only a cache the
	# caller alone can write is used.
	if ! mkdir -p -m 0700 "$cache_root" 2>/dev/null || [ -L "$cache_root" ] ||
	    [ "$(stat -c %u "$cache_root" 2>/dev/null)" != "$(id -u)" ] ||
	    [ $(( 0$(stat -c %a "$cache_root" 2>/dev/null) & 022 )) -ne 0 ]; then
		echo "sign-darling-runtime.sh: not using signing cache $cache_root (missing or writable by others)" >&2
		cache_root=""
	fi
fi

counts_file=$tmp/counts
py_rc=0
# shellcheck disable=SC2086
SIGN_TMP="$tmp" \
SIGN_TRUST_ENTRIES="$trust_entries" \
SIGN_COUNTS="$counts_file" \
SIGN_DCC="$DCC" \
SIGN_RCODESIGN="$RCODESIGN" \
SIGN_LIPO="$LIPO" \
SIGN_DRY="$dry" \
SIGN_FORCE="$force" \
SIGN_VERIFY="$verify" \
SIGN_THIN="$thin" \
SIGN_TRUST="$trust" \
SIGN_SDK_SELECTED="$sdk_selected" \
SIGN_NO_TRUST="$no_trust" \
SIGN_QUIET="$quiet" \
SIGN_CACHE_ROOT="$cache_root" \
python3 - $roots <<'PY' || py_rc=$?
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

# Paths are filesystem bytes decoded with surrogateescape; print them back unchanged.
sys.stdout.reconfigure(errors='surrogateescape')
sys.stderr.reconfigure(errors='surrogateescape')

SDK_PATH = '/usr/local/libexec/darling'
TMP_DIR = os.environ['SIGN_TMP']
TRUST_ENTRIES = os.environ['SIGN_TRUST_ENTRIES']
COUNTS_FILE = os.environ['SIGN_COUNTS']
DCC = os.environ['SIGN_DCC']
RCODESIGN = os.environ['SIGN_RCODESIGN']
LIPO = os.environ['SIGN_LIPO']
DRY = os.environ['SIGN_DRY'] == '1'
FORCE = os.environ['SIGN_FORCE'] == '1'
VERIFY = os.environ['SIGN_VERIFY'] == '1'
THIN = os.environ['SIGN_THIN'] == '1'
TRUST = os.environ['SIGN_TRUST'] == '1'
SDK_SELECTED = os.environ['SIGN_SDK_SELECTED'] == '1'
NO_TRUST = os.environ['SIGN_NO_TRUST'] == '1'
QUIET = os.environ['SIGN_QUIET'] == '1'
CACHE_ROOT = os.environ['SIGN_CACHE_ROOT']


def worker_count():
    raw = os.environ.get('RCLOL_SIGN_JOBS', '')
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return max(1, min(os.cpu_count() or 4, 16))


def scan_macho(target):
    if os.path.isfile(target) and not os.path.islink(target):
        entries = [target]
    else:
        entries = []
        for top, dirs, files in os.walk(target):
            dirs.sort()
            for name in sorted(files):
                entries.append(os.path.join(top, name))
    found = []
    for path in entries:
        if '\n' in path or '\t' in path:
            continue
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            with open(path, 'rb') as handle:
                head = handle.read(16)
        except OSError:
            continue
        magic = head[:4]
        if magic in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe'):
            kind = 'object' if head[12:16] == b'\x01\x00\x00\x00' else 'thin'
        elif magic in (b'\xfe\xed\xfa\xcf', b'\xfe\xed\xfa\xce'):
            kind = 'object' if head[12:16] == b'\x00\x00\x00\x01' else 'thin'
        elif magic in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
            kind = 'fat'
        else:
            continue
        found.append((kind, path))
    return found


def file_sha256(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def cache_key_for(path):
    try:
        content = file_sha256(path)
    except OSError:
        return ''
    ident = hashlib.sha256(os.fsencode(os.path.basename(path))).hexdigest()[:16]
    return f'{content}-{ident}'


def copy_metadata(ref_stat, dst):
    try:
        os.chmod(dst, stat.S_IMODE(ref_stat.st_mode))
    except OSError:
        pass
    try:
        os.chown(dst, ref_stat.st_uid, ref_stat.st_gid)
    except OSError:
        pass


def dcc_ok(*args):
    try:
        return subprocess.run(
            [DCC, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
    except OSError:
        return False


def cdhash_of(path):
    try:
        res = subprocess.run(
            [DCC, 'cdhash', path], capture_output=True, text=True
        )
    except OSError:
        return ''
    if res.returncode != 0:
        return ''
    return (res.stdout or '').strip()


def restore_cached(target, cache_path):
    if not os.path.isfile(cache_path) or os.path.islink(cache_path):
        return False
    if not dcc_ok('info', cache_path):
        return False
    parent = os.path.dirname(target) or '.'
    tmpf = None
    try:
        ref_stat = os.lstat(target)
        fd, tmpf = tempfile.mkstemp(dir=parent, prefix='.dc-cache.')
        os.close(fd)
        shutil.copyfile(cache_path, tmpf)
        if VERIFY and not dcc_ok('verify', tmpf):
            os.unlink(tmpf)
            return False
        copy_metadata(ref_stat, tmpf)
        os.replace(tmpf, target)
        return True
    except OSError:
        if tmpf is not None:
            try:
                os.unlink(tmpf)
            except OSError:
                pass
        return False


def store_cached(signed_file, cache_path):
    cache_dir = os.path.dirname(cache_path)
    part = None
    try:
        fd, part = tempfile.mkstemp(dir=cache_dir, prefix='.part.')
        os.close(fd)
        shutil.copyfile(signed_file, part)
        os.chmod(part, 0o600)
        os.replace(part, cache_path)
    except OSError:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass


def prune_cache(cache_dir, kept_paths, new_entries):
    if not cache_dir:
        return
    index_path = os.path.join(cache_dir, 'index')
    merged = {}
    try:
        with open(index_path, 'rb') as handle:
            for raw in handle:
                raw = raw.rstrip(b'\n')
                if b'\t' not in raw:
                    continue
                k_bytes, p_bytes = raw.split(b'\t', 1)
                p = os.fsdecode(p_bytes)
                if p in kept_paths:
                    merged[p] = k_bytes.decode('ascii', 'replace')
    except OSError:
        pass
    for k, p in new_entries:
        merged[p] = k
    lines = sorted(k.encode('ascii', 'replace') + b'\t' + os.fsencode(p)
                   for p, k in merged.items())
    part = None
    try:
        fd, part = tempfile.mkstemp(dir=cache_dir, prefix='.index.')
        with os.fdopen(fd, 'wb') as handle:
            if lines:
                handle.write(b'\n'.join(lines) + b'\n')
        os.replace(part, index_path)
    except OSError:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass
        return
    active_keys = set(merged.values())
    try:
        with os.scandir(cache_dir) as scan:
            for entry in scan:
                if entry.name == 'index' or entry.name.startswith('.'):
                    continue
                if entry.is_file(follow_symlinks=False) and entry.name not in active_keys:
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
    except OSError:
        pass


def process_one(kind, f, cache_dir, trust_this_root):
    res = {
        'total': 1, 'signed': 0, 'restored': 0, 'skipped': 0,
        'fat': 0, 'thinned': 0, 'object': 0, 'failed': 0,
        'out': [], 'err': [], 'kept': None, 'new_index': None,
        'trust_hash': None,
    }
    backup = None
    try:
        if kind == 'fat':
            archs = ''
            if LIPO:
                try:
                    out = subprocess.run(
                        [LIPO, '-archs', f], capture_output=True, text=True
                    )
                    if out.returncode == 0:
                        archs = (out.stdout or '').replace('\n', '').strip()
                except OSError:
                    archs = ''
            if THIN and archs == 'x86_64':
                if DRY:
                    res['thinned'] = 1
                    if not QUIET:
                        res['out'].append(f'would thin: {f}')
                else:
                    try:
                        fd, backup = tempfile.mkstemp(dir=TMP_DIR, prefix='fat-backup.')
                        os.close(fd)
                        shutil.copy2(f, backup)
                    except OSError:
                        res['err'].append(f'thin FAILED (backup): {f}')
                        res['failed'] = 1
                        return res
                    tmpf = None
                    try:
                        ref_stat = os.lstat(f)
                        fd, tmpf = tempfile.mkstemp(dir=os.path.dirname(f) or '.', prefix='.dc-thin.')
                        os.close(fd)
                        thin_run = subprocess.run(
                            [LIPO, '-thin', 'x86_64', f, '-output', tmpf],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                        if thin_run.returncode != 0:
                            os.unlink(tmpf)
                            res['err'].append(f'thin FAILED: {f}')
                            res['failed'] = 1
                            return res
                        copy_metadata(ref_stat, tmpf)
                        os.replace(tmpf, f)
                    except OSError:
                        if tmpf is not None:
                            try:
                                os.unlink(tmpf)
                            except OSError:
                                pass
                        res['err'].append(f'thin FAILED (temp): {f}')
                        res['failed'] = 1
                        return res
                    res['thinned'] = 1
                    if not QUIET:
                        res['out'].append(f'thinned: {f}')
            else:
                res['fat'] = 1
                if not QUIET:
                    suffix = f' ({archs})' if archs else ''
                    res['out'].append(f'skip fat{suffix}: {f}')
                return res

        if kind == 'object':
            res['object'] = 1
            if not QUIET:
                res['out'].append(f'skip object: {f}')
            return res

        if not FORCE and dcc_ok('info', f):
            if VERIFY and not dcc_ok('verify', f):
                res['err'].append(f'verify FAILED (existing): {f}')
                res['failed'] = 1
                return res
            res['skipped'] = 1
            res['kept'] = f
            if trust_this_root and not DRY:
                h = cdhash_of(f)
                if h:
                    res['trust_hash'] = h
                else:
                    res['failed'] = 1
            return res

        if DRY:
            res['out'].append(f'would sign: {f}')
            res['signed'] = 1
            return res

        key = cache_key_for(f) if cache_dir else ''
        if key and restore_cached(f, os.path.join(cache_dir, key)):
            res['restored'] = 1
            res['new_index'] = (key, f)
            if trust_this_root:
                h = cdhash_of(f)
                if h:
                    res['trust_hash'] = h
                else:
                    res['failed'] = 1
            if not QUIET:
                res['out'].append(f'restored: {f}')
            return res

        try:
            sign_rc = subprocess.run(
                [RCODESIGN, 'sign', '--binary-identifier', os.path.basename(f), f],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).returncode
        except OSError:
            sign_rc = 1

        if sign_rc == 0:
            if VERIFY and not dcc_ok('verify', f):
                res['err'].append(f'verify FAILED: {f}')
                if backup:
                    try:
                        shutil.copy2(backup, f)
                    except OSError:
                        pass
                res['failed'] = 1
                return res
            res['signed'] = 1
            if key and cache_dir:
                store_cached(f, os.path.join(cache_dir, key))
                res['new_index'] = (key, f)
            if trust_this_root:
                h = cdhash_of(f)
                if h:
                    res['trust_hash'] = h
                else:
                    res['failed'] = 1
            if not QUIET:
                res['out'].append(f'signed: {f}')
        else:
            res['err'].append(f'sign FAILED: {f}')
            if backup:
                try:
                    shutil.copy2(backup, f)
                except OSError:
                    pass
            res['failed'] = 1
        return res
    finally:
        if backup:
            try:
                os.unlink(backup)
            except OSError:
                pass


counts = {
    'total': 0, 'signed': 0, 'restored': 0, 'skipped': 0,
    'fat': 0, 'thinned': 0, 'object': 0, 'failed': 0,
}
jobs = worker_count()
trust_lines = []

for root in sys.argv[1:]:
    if not os.path.isdir(root) and not os.path.isfile(root):
        sys.stderr.write(f'sign-darling-runtime.sh: not a file or directory: {root}\n')
        counts['failed'] += 1
        continue
    if not QUIET:
        sys.stdout.write(f'== {root} ==\n')
    entries = scan_macho(root)
    cache_dir = ''
    if CACHE_ROOT:
        cache_dir = os.path.join(CACHE_ROOT, hashlib.sha256(os.fsencode(root)).hexdigest()[:16])
        try:
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        except OSError:
            cache_dir = ''
    trust_this_root = TRUST or (SDK_SELECTED and not NO_TRUST and root == SDK_PATH)

    # Names that share a device and inode (hard links) are signed in place by
    # rcodesign, so one lock per inode serializes them while other files stay parallel.
    locks = {}
    work = []
    for kind, path in entries:
        try:
            info = os.lstat(path)
            key = (info.st_dev, info.st_ino)
        except OSError:
            key = ('path', path)
        work.append((kind, path, locks.setdefault(key, threading.Lock())))

    def run_locked(item):
        with item[2]:
            return process_one(item[0], item[1], cache_dir, trust_this_root)

    if jobs > 1 and len(work) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(run_locked, work))
    else:
        results = [run_locked(item) for item in work]

    kept_paths = set()
    new_entries = []
    for res in results:
        for line in res['out']:
            sys.stdout.write(line + '\n')
        for line in res['err']:
            sys.stderr.write(line + '\n')
        for key_name in counts:
            counts[key_name] += res[key_name]
        if res['kept']:
            kept_paths.add(res['kept'])
        if res['new_index']:
            new_entries.append(res['new_index'])
        if res['trust_hash']:
            trust_lines.append(f'{res["trust_hash"]} 1\n')
    prune_cache(cache_dir, kept_paths, new_entries)

if trust_lines:
    with open(TRUST_ENTRIES, 'a', encoding='utf-8') as handle:
        handle.writelines(trust_lines)

with open(COUNTS_FILE, 'w', encoding='utf-8') as handle:
    for key_name, value in counts.items():
        handle.write(f'{key_name}={value}\n')
PY

if [ "$py_rc" -ne 0 ] || [ ! -f "$counts_file" ]; then
	echo "sign-darling-runtime.sh: signing worker failed (exit $py_rc)" >&2
	[ "$py_rc" -ne 0 ] && exit "$py_rc"
	exit 1
fi

total=0; signed=0; restored=0; skipped=0; fat=0; thinned=0; object=0; failed=0
# shellcheck disable=SC1090
. "$counts_file"

if [ "$dry" -eq 0 ] && [ -s "$trust_entries" ]; then
	if [ -z "$trust_store" ]; then
		trust_store=$(python3 "$DCT" default-store) || {
			echo "trust-store FAILED: cannot determine default store" >&2
			failed=$((failed + 1))
			trust_store=""
		}
	fi
	if [ -n "$trust_store" ]; then
		if ! python3 "$DCT" sync "$trust_store" --from "$trust_entries"; then
			echo "trust-store FAILED: $trust_store" >&2
			failed=$((failed + 1))
		fi
	fi
fi

printf -- '----\n'
printf 'total=%d signed=%d restored=%d skipped=%d fat=%d thinned=%d object=%d failed=%d%s\n' \
	"$total" "$signed" "$restored" "$skipped" "$fat" "$thinned" "$object" "$failed" \
	"$([ "$dry" -eq 1 ] && echo ' (dry-run)')"
[ "$failed" -eq 0 ]
