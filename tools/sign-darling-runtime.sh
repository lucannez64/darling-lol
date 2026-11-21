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
		for d in "$prefix_root"/Users/*/RuntimeLibraries "$prefix_root"/Users/*/RuntimeFrameworks "$prefix_root"/usr/lib/system; do
			[ -d "$d" ] && roots="$roots $d"
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
	for d in "$prefix_root"/Users/*/RuntimeLibraries "$prefix_root"/Users/*/RuntimeFrameworks "$prefix_root"/usr/lib/system; do
		[ -d "$d" ] && roots="$roots $d"
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

trust_hash() {
	# Automatic trust is confined to the SDK, even with --sdk --prefix.
	[ "$trust" -eq 1 ] || {
		[ "$sdk_selected" -eq 1 ] && [ "$no_trust" -eq 0 ] &&
		    [ "$root" = /usr/local/libexec/darling ]
	} || return 0
	[ "$dry" -eq 0 ] || return 0
	hash=$("$DCC" cdhash "$1" 2>/dev/null) || return 1
	[ -n "$hash" ] || return 1
	printf '%s 1\n' "$hash" >> "$trust_entries"
}

# Classifies every regular file of a root in one pass and prints
# "kind<TAB>path" for the Mach-O ones (thin, object or fat). Spawning od for each
# of the SDK's ~33,000 files made a scan take about a minute even when nothing
# needed signing. MH_OBJECT is filetype 1 at offset 12, in the file's byte order.
scan_macho() {
	python3 - "$1" <<'PY'
import os
import stat
import sys

for top, dirs, files in os.walk(sys.argv[1]):
    dirs.sort()
    for name in sorted(files):
        path = os.path.join(top, name)
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
        sys.stdout.write(kind + '\t' + path + '\n')
PY
}

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
	if ! mkdir -p "$cache_root" 2>/dev/null || [ -L "$cache_root" ] ||
	    [ "$(stat -c %u "$cache_root" 2>/dev/null)" != "$(id -u)" ] ||
	    [ $(( 0$(stat -c %a "$cache_root" 2>/dev/null) & 022 )) -ne 0 ]; then
		echo "sign-darling-runtime.sh: not using signing cache $cache_root (missing or writable by others)" >&2
		cache_root=""
	fi
fi

cache_key() { # file -> key of its unsigned content and signing identifier
	content=$(sha256sum < "$1" 2>/dev/null | cut -c1-64) || return 1
	identifier=$(printf '%s' "$(basename "$1")" | sha256sum | cut -c1-16)
	[ ${#content} -eq 64 ] || return 1
	printf '%s-%s\n' "$content" "$identifier"
}

# Replaces $1 with the cached signed copy $2 once that copy verifies.
restore_cached() {
	[ -f "$2" ] && [ ! -L "$2" ] || return 1
	"$DCC" info "$2" >/dev/null 2>&1 || return 1
	tmpf=$(mktemp "$(dirname "$1")/.dc-cache.XXXXXX") || return 1
	if ! cp "$2" "$tmpf" || { [ "$verify" -eq 1 ] && ! "$DCC" verify "$tmpf" >/dev/null 2>&1; }; then
		rm -f "$tmpf"
		return 1
	fi
	chmod --reference="$1" "$tmpf" 2>/dev/null || true
	chown --reference="$1" "$tmpf" 2>/dev/null || true
	mv -f "$tmpf" "$1"
}

store_cached() { # signed file, cache path
	part=$(mktemp "$(dirname "$2")/.part.XXXXXX") || return 0
	if cp "$1" "$part"; then
		chmod 0600 "$part"
		mv -f "$part" "$2"
	else
		rm -f "$part"
	fi
}

# Keeps the entries of this root's cache that its binaries still use: the ones
# signed or restored now, and the index entries of binaries already signed.
prune_cache() { # cache dir
	[ -n "$1" ] || return 0
	index=$1/index
	[ -f "$index" ] || : > "$index"
	awk -F '\t' 'NR == FNR { keep[$0] = 1; next } ($2 in keep)' \
		"$tmp/kept-paths" "$index" > "$tmp/carried"
	cat "$tmp/new-index" "$tmp/carried" | sort -u -t "$(printf '\t')" -k2,2 > "$tmp/index"
	cp "$tmp/index" "$index.part" && mv -f "$index.part" "$index"
	cut -f1 "$tmp/index" | sort -u > "$tmp/keys"
	find "$1" -maxdepth 1 -type f ! -name index ! -name '.*' -printf '%f\n' | sort > "$tmp/present"
	comm -23 "$tmp/present" "$tmp/keys" | while IFS= read -r stale; do
		rm -f "$1/$stale"
	done
}

signed=0; restored=0; skipped=0; failed=0; fat=0; thinned=0; object=0; total=0

for root in $roots; do
	if [ ! -d "$root" ]; then
		echo "sign-darling-runtime.sh: not a directory: $root" >&2
		failed=$((failed + 1))
		continue
	fi
	[ "$quiet" -eq 1 ] || echo "== $root =="
	scan_macho "$root" > "$tmp/files" 2>/dev/null || true
	cache_dir=""
	if [ -n "$cache_root" ]; then
		cache_dir=$cache_root/$(printf '%s' "$root" | sha256sum | cut -c1-16)
		mkdir -p -m 0700 "$cache_dir" 2>/dev/null || cache_dir=""
	fi
	: > "$tmp/new-index"
	: > "$tmp/kept-paths"
	while IFS='	' read -r kind f; do
		total=$((total + 1))
		backup=""
		if [ "$kind" = fat ]; then
			archs=""
			[ -n "$LIPO" ] && archs=$("$LIPO" -archs "$f" 2>/dev/null | tr -d '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
			if [ "$thin" -eq 1 ] && [ "$archs" = x86_64 ]; then
				if [ "$dry" -eq 1 ]; then
					thinned=$((thinned + 1))
					[ "$quiet" -eq 1 ] || echo "would thin: $f"
				else
					backup="$tmp/fat-backup.$$"
					rm -f "$backup"
					cp -a "$f" "$backup" || {
						echo "thin FAILED (backup): $f" >&2
						failed=$((failed + 1))
						continue
					}
					tmpf=$(mktemp "$(dirname "$f")/.dc-thin.XXXXXX") || {
						echo "thin FAILED (temp): $f" >&2
						rm -f "$backup"
						failed=$((failed + 1))
						continue
					}
					if ! "$LIPO" -thin x86_64 "$f" -output "$tmpf"; then
						rm -f "$tmpf" "$backup"
						echo "thin FAILED: $f" >&2
						failed=$((failed + 1))
						continue
					fi
					chmod --reference="$f" "$tmpf" 2>/dev/null || true
					chown --reference="$f" "$tmpf" 2>/dev/null || true
					mv -f "$tmpf" "$f"
					thinned=$((thinned + 1))
					[ "$quiet" -eq 1 ] || echo "thinned: $f"
				fi
			else
				fat=$((fat + 1))
				[ "$quiet" -eq 1 ] || echo "skip fat${archs:+ ($archs)}: $f"
				continue
			fi
		fi
		if [ "$kind" = object ]; then
			object=$((object + 1))
			[ "$quiet" -eq 1 ] || echo "skip object: $f"
			continue
		fi
		if [ "$force" -eq 0 ] && "$DCC" info "$f" >/dev/null 2>&1; then
			if [ "$verify" -eq 1 ] && ! "$DCC" verify "$f" >/dev/null 2>&1; then
				echo "verify FAILED (existing): $f" >&2
				failed=$((failed + 1))
				[ -n "$backup" ] && rm -f "$backup"
				continue
			fi
			skipped=$((skipped + 1))
			printf '%s\n' "$f" >> "$tmp/kept-paths"
			trust_hash "$f" || failed=$((failed + 1))
			[ -n "$backup" ] && rm -f "$backup"
			continue
		fi
		if [ "$dry" -eq 1 ]; then
			echo "would sign: $f"
			signed=$((signed + 1))
			continue
		fi
		key=""
		if [ -n "$cache_dir" ]; then
			key=$(cache_key "$f") || key=""
		fi
		if [ -n "$key" ] && restore_cached "$f" "$cache_dir/$key"; then
			restored=$((restored + 1))
			printf '%s\t%s\n' "$key" "$f" >> "$tmp/new-index"
			trust_hash "$f" || failed=$((failed + 1))
			[ -n "$backup" ] && rm -f "$backup"
			[ "$quiet" -eq 1 ] || echo "restored: $f"
			continue
		fi
		if "$RCODESIGN" sign --binary-identifier "$(basename "$f")" "$f" >/dev/null 2>&1; then
			if [ "$verify" -eq 1 ] && ! "$DCC" verify "$f" >/dev/null 2>&1; then
				echo "verify FAILED: $f" >&2
				if [ -n "$backup" ]; then
					cp -a "$backup" "$f"
					rm -f "$backup"
				fi
				failed=$((failed + 1))
				continue
			fi
			signed=$((signed + 1))
			if [ -n "$key" ]; then
				store_cached "$f" "$cache_dir/$key"
				printf '%s\t%s\n' "$key" "$f" >> "$tmp/new-index"
			fi
			trust_hash "$f" || failed=$((failed + 1))
			[ -n "$backup" ] && rm -f "$backup"
			[ "$quiet" -eq 1 ] || echo "signed: $f"
		else
			echo "sign FAILED: $f" >&2
			if [ -n "$backup" ]; then
				cp -a "$backup" "$f"
				rm -f "$backup"
			fi
			failed=$((failed + 1))
		fi
	done < "$tmp/files"
	prune_cache "$cache_dir"
done

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
