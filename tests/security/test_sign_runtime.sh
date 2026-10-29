#!/bin/sh
# Tests automatic thinning in tools/sign-darling-runtime.sh.
#
# Requires:
#   - tools/dc-codesign/dc-codesign (make -C tools/dc-codesign)
#   - rcodesign (apple-codesign)
#   - llvm-lipo (or lipo)
#   - a thin Mach-O in the build tree
#
# The test never touches the installed runtime: all fixtures live in a temp
# directory.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
signer=$repo/tools/sign-darling-runtime.sh
DCC=${DCC:-$repo/tools/dc-codesign/dc-codesign}
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi
LIPO=${LIPO:-$(command -v llvm-lipo 2>/dev/null || command -v lipo 2>/dev/null || true)}

pass=0; fail=0; skip=0
ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail+1)); }
miss() { printf 'SKIP  %s\n' "$1"; skip=$((skip+1)); }

finish() {
	printf -- '----\n'
	printf 'pass=%d fail=%d skip=%d\n' "$pass" "$fail" "$skip"
	[ "$fail" -eq 0 ]
}

for tool in "$DCC" "$RCODESIGN" "$LIPO"; do
	if [ ! -x "$tool" ]; then
		miss "$(basename "$tool") not found"
		finish
		exit $?
	fi
done

thin=$(find "$repo/build/darling-src/build" -type f -name '*.dylib' 2>/dev/null | while IFS= read -r f; do
	if file -b "$f" 2>/dev/null | grep -q 'Mach-O'; then printf '%s\n' "$f"; break; fi
done)
if [ -z "$thin" ]; then
	miss "no thin Mach-O in the build tree"
	finish
	exit $?
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cp "$thin" "$tmp/thin.dylib"
cp "$thin" "$tmp/fat-one.dylib"
"$LIPO" -create -arch x86_64 "$tmp/thin.dylib" -output "$tmp/fat-one.dylib" >/dev/null 2>&1 \
	|| { miss "could not create a single-architecture fat fixture"; finish; exit $?; }
cp "$thin" "$tmp/fat-multi.dylib"
cp "$thin" "$tmp/fake-arm64.dylib"
python3 - "$tmp/fake-arm64.dylib" <<'PY' || { miss "could not create a multi-architecture fixture"; finish; exit $?; }
import struct, sys
path = sys.argv[1]
data = bytearray(open(path, 'rb').read())
# Mach-O 64-bit little-endian: magic, cputype, cpusubtype at offsets 0/4/8.
if data[:4] != b'\xcf\xfa\xed\xfe':
    raise SystemExit(1)
struct.pack_into('<II', data, 4, 0x0100000c, 0)
open(path, 'wb').write(data)
PY
"$LIPO" -create -arch x86_64 "$tmp/thin.dylib" -arch arm64 "$tmp/fake-arm64.dylib" \
	-output "$tmp/fat-multi.dylib" >/dev/null 2>&1 \
	|| { miss "could not create a multi-architecture fat fixture"; finish; exit $?; }
rm -f "$tmp/fake-arm64.dylib"

cp "$thin" "$tmp/signed.dylib"
"$RCODESIGN" sign --binary-identifier com.example.signed "$tmp/signed.dylib" >/dev/null 2>&1 \
	|| { miss "rcodesign failed to sign the idempotence fixture"; finish; exit $?; }

object=
for candidate in \
	/usr/local/libexec/darling/System/Library/Frameworks/Python.framework/Versions/2.7/lib/python2.7/config/python.o \
	$(find "$repo/build/darling-src/build" -type f -name '*.o' 2>/dev/null | head -20); do
	if [ -f "$candidate" ] && file -b "$candidate" 2>/dev/null | grep -q 'Mach-O.*object'; then
		object=$candidate
		break
	fi
done

object_count=0
total=4
if [ -n "$object" ]; then
	cp "$object" "$tmp/object.o"
	object_count=1
	total=5
fi

run() { # extra args...
	# Fixtures are isolated in $tmp; do not depend on whether a live prefix
	# happens to have darlingserver running.
	RCODESIGN="$RCODESIGN" LIPO="$LIPO" "$signer" --allow-running --quiet "$@" "$tmp"
}

out=$(run 2>&1)
case "$out" in
*total="$total"*signed=2*skipped=1*fat=1*thinned=1*object="$object_count"*failed=0*)
	ok "first run thins one fat, signs it, skips one fat/object/signed" ;;
*)
	bad "first run summary: $out" ;;
esac
if [ "$(od -An -tx1 -N4 "$tmp/fat-one.dylib" | tr -d ' \n')" = cffaedfe ] \
   && "$DCC" verify "$tmp/fat-one.dylib" >/dev/null 2>&1; then
	ok "thinned fixture is thin and verifies"
else
	bad "thinned fixture is not a verified thin Mach-O"
fi
if [ "$(od -An -tx1 -N4 "$tmp/fat-multi.dylib" | tr -d ' \n')" = cafebabe ]; then
	ok "multi-architecture fat is untouched"
else
	bad "multi-architecture fat was modified"
fi

out=$(run 2>&1)
case "$out" in
*total="$total"*signed=0*skipped=3*fat=1*thinned=0*object="$object_count"*failed=0*)
	ok "second run is idempotent" ;;
*)
	bad "second run summary: $out" ;;
esac

cp "$thin" "$tmp/no-thin-in.dylib"
"$LIPO" -create -arch x86_64 "$tmp/no-thin-in.dylib" -output "$tmp/no-thin.dylib" >/dev/null 2>&1
rm -f "$tmp/no-thin-in.dylib"
out=$(run --no-thin 2>&1)
case "$out" in
*total=$((total + 1))*thinned=0*failed=0*)
	ok "--no-thin leaves the fat binary alone" ;;
*)
	bad "--no-thin summary: $out" ;;
esac
if [ "$(od -An -tx1 -N4 "$tmp/no-thin.dylib" | tr -d ' \n')" = cafebabe ]; then
	ok "--no-thin fixture remains fat"
else
	bad "--no-thin fixture was thinned"
fi

finish
