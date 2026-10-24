#!/bin/sh
# Ad-hoc sign a Darling runtime tree so the M2 loader gate can enforce.
#
# Usage:
#   sign-darling-runtime.sh [options] [DIR...]
#
#   --sdk             add /usr/local/libexec/darling (needs root)
#   --prefix          add the per-user prefix overlays (RuntimeLibraries,
#                     RuntimeFrameworks) under ~/.darling
#   --dry-run         list what would be signed
#   --force           re-sign files that already have a signature
#   --verify          verify after signing (default)
#   --no-verify       skip the post-signing dc-codesign verify
#   --no-thin         do not thin single-architecture x86_64 fat binaries
#   --allow-running   permit signing while darlingserver is running
#   --quiet           only print the summary
#
# With no DIR and no --sdk/--prefix, both defaults are used.
#
# Idempotent: files that already carry a CodeDirectory are skipped unless
# --force. Relocatable objects (MH_OBJECT) are skipped because they are not
# loadable images. A fat binary whose only slice is x86_64 is thinned in place
# (the guest target is thin x86-64) and then signed; multi-architecture fat
# binaries are skipped.
#
# Exit: 0 all good, 1 some file failed, 2 usage/setup error.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DCC=${DCC:-$here/dc-codesign/dc-codesign}
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi
LIPO=${LIPO:-$(command -v llvm-lipo 2>/dev/null || command -v lipo 2>/dev/null || true)}

dry=0
force=0
verify=1
thin=1
allow_running=0
quiet=0
roots=""
explicit=0

usage() {
	sed -n "2,/^$/p" "$0" | sed 's/^# \{0,1\}//'
	exit 2
}

while [ $# -gt 0 ]; do
	case "$1" in
	--sdk) roots="$roots /usr/local/libexec/darling"; explicit=1 ;;
	--prefix)
		for d in "$HOME"/.darling/Users/*/RuntimeLibraries "$HOME"/.darling/Users/*/RuntimeFrameworks; do
			[ -d "$d" ] && roots="$roots $d"
		done
		explicit=1 ;;
	--dry-run) dry=1 ;;
	--force) force=1 ;;
	--verify) verify=1 ;;
	--no-verify) verify=0 ;;
	--no-thin) thin=0 ;;
	--allow-running) allow_running=1 ;;
	--quiet) quiet=1 ;;
	-h|--help) usage ;;
	-*) echo "sign-darling-runtime.sh: unknown option $1" >&2; usage ;;
	*) roots="$roots $1"; explicit=1 ;;
	esac
	shift
done

if [ "$explicit" -eq 0 ]; then
	roots="/usr/local/libexec/darling"
	for d in "$HOME"/.darling/Users/*/RuntimeLibraries "$HOME"/.darling/Users/*/RuntimeFrameworks; do
		[ -d "$d" ] && roots="$roots $d"
	done
fi

if [ ! -f "$DCC" ] || [ ! -x "$DCC" ]; then
	echo "sign-darling-runtime.sh: dc-codesign not built at $DCC" >&2
	echo "  run: make -C $here" >&2
	exit 2
fi
if [ -z "$RCODESIGN" ] || [ ! -x "$RCODESIGN" ]; then
	echo "sign-darling-runtime.sh: rcodesign not found (install apple-codesign)" >&2
	exit 2
fi

if [ "$allow_running" -eq 0 ] && [ "$dry" -eq 0 ] && pgrep -x darlingserver >/dev/null 2>&1; then
	echo "sign-darling-runtime.sh: darlingserver is running; run 'darling shutdown' first" >&2
	echo "  (or pass --allow-running if you know the tree is not in use)" >&2
	exit 2
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

is_macho() { # path -> prints thin|fat|object|no
	magic=$(od -An -tx1 -N4 "$1" 2>/dev/null | tr -d ' \n')
	case "$magic" in
	cffaedfe|cefaedfe)
		# Little-endian 32/64-bit Mach-O; MH_OBJECT has filetype 1 at $12.
		if [ "$(od -An -tx1 -j12 -N4 "$1" 2>/dev/null | tr -d ' \n')" = 01000000 ]; then
			echo object
		else
			echo thin
		fi ;;
	feedfacf|feedface)
		# Big-endian 32/64-bit Mach-O.
		if [ "$(od -An -tx1 -j12 -N4 "$1" 2>/dev/null | tr -d ' \n')" = 00000001 ]; then
			echo object
		else
			echo thin
		fi ;;
	cafebabe|bebafeca) echo fat ;;
	*) echo no ;;
	esac
}

signed=0; skipped=0; failed=0; fat=0; thinned=0; object=0; total=0

for root in $roots; do
	if [ ! -d "$root" ]; then
		echo "sign-darling-runtime.sh: not a directory: $root" >&2
		failed=$((failed + 1))
		continue
	fi
	[ "$quiet" -eq 1 ] || echo "== $root =="
	find "$root" -type f > "$tmp/files" 2>/dev/null || true
	while IFS= read -r f; do
		kind=$(is_macho "$f")
		[ "$kind" = no ] && continue
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
			skipped=$((skipped + 1))
			[ -n "$backup" ] && rm -f "$backup"
			continue
		fi
		if [ "$dry" -eq 1 ]; then
			echo "would sign: $f"
			signed=$((signed + 1))
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
done

printf -- '----\n'
printf 'total=%d signed=%d skipped=%d fat=%d thinned=%d object=%d failed=%d%s\n' \
	"$total" "$signed" "$skipped" "$fat" "$thinned" "$object" "$failed" \
	"$([ "$dry" -eq 1 ] && echo ' (dry-run)')"
[ "$failed" -eq 0 ]
