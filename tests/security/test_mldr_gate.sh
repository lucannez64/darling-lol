#!/bin/sh
# Tests the M2 loader gate in a built Darling tree.
#
# Requires:
#   - build/darling-src/build/src/startup/mldr/mldr (cmake --build ... --target mldr)
#   - rcodesign for the signed/tampered fixtures
#
# The gate runs before the loader, so these cases are observable without a
# running Darling namespace: the load itself may fail afterwards, but the gate
# decision is already logged.
#
# Usage: ./test_mldr_gate.sh [path/to/mldr]

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
MLDR=${1:-${MLDR:-$repo/build/darling-src/build/src/startup/mldr/mldr}}
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi

pass=0; fail=0; skip=0
ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail+1)); }
miss() { printf 'SKIP  %s\n' "$1"; skip=$((skip+1)); }

if [ ! -x "$MLDR" ]; then
	miss "mldr not built ($MLDR); run: cmake --build build/darling-src/build --target mldr"
	echo "----"; echo "pass=$pass fail=$fail skip=$skip"; exit 0
fi
if [ -z "$RCODESIGN" ]; then
	miss "rcodesign not found; cannot build signed fixtures"
	echo "----"; echo "pass=$pass fail=$fail skip=$skip"; exit 0
fi

unsigned=$(find "$repo/build/darling-src/build" -type f -name '*.dylib' 2>/dev/null | while IFS= read -r f; do
	if file -b "$f" 2>/dev/null | grep -q 'Mach-O'; then printf '%s\n' "$f"; break; fi
done)
if [ -z "$unsigned" ]; then
	miss "no unsigned Mach-O in the build tree"
	echo "----"; echo "pass=$pass fail=$fail skip=$skip"; exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cp "$unsigned" "$tmp/unsigned.dylib"
cp "$unsigned" "$tmp/signed.dylib"
"$RCODESIGN" sign --binary-identifier com.example.gate "$tmp/signed.dylib" >/dev/null 2>&1 \
	|| { miss "rcodesign failed to sign the fixture"; echo "----"; echo "pass=$pass fail=$fail skip=$skip"; exit 0; }
cp "$tmp/signed.dylib" "$tmp/tampered.dylib"
printf '\377' | dd of="$tmp/tampered.dylib" bs=1 seek=100 conv=notrunc status=none 2>/dev/null

run() { # policy file outfile
	DARLING_CODESIGN_ENFORCE="$1" DARLING_CODESIGN_VERBOSE=1 \
		timeout 10 "$MLDR" "$2" >"$3" 2>&1
}

expect() { # label file needle
	if grep -qa "$3" "$2"; then ok "$1"; else bad "$1 (missing: $3)"; fi
}
reject() { # label file needle
	if grep -qa "$3" "$2"; then bad "$1 (unexpected: $3)"; else ok "$1"; fi
}

# off: the gate must stay silent, regardless of the signature.
run off "$tmp/unsigned.dylib" "$tmp/off.log"
reject "off: unsigned image not checked" "$tmp/off.log" "codesign"

# warn: reports, continues.
run warn "$tmp/unsigned.dylib" "$tmp/warn.log"
expect "warn: unsigned image reports" "$tmp/warn.log" "codesign warning"
reject "warn: does not refuse" "$tmp/warn.log" "refusing to load"

# enforce: refuses unsigned.
run enforce "$tmp/unsigned.dylib" "$tmp/enforce-unsigned.log"
expect "enforce: unsigned image rejected" "$tmp/enforce-unsigned.log" "codesign rejected"
expect "enforce: loader refuses" "$tmp/enforce-unsigned.log" "refusing to load"

# enforce: accepts a valid ad-hoc signature.
run enforce "$tmp/signed.dylib" "$tmp/enforce-signed.log"
expect "enforce: signed image verifies" "$tmp/enforce-signed.log" "codesign ok"
reject "enforce: signed image not refused" "$tmp/enforce-signed.log" "refusing to load"

# enforce: follows a symlink to the signed image, like execve does.
ln -s signed.dylib "$tmp/signed-link.dylib"
run enforce "$tmp/signed-link.dylib" "$tmp/enforce-symlink.log"
expect "enforce: signed symlink verifies" "$tmp/enforce-symlink.log" "codesign ok"
reject "enforce: signed symlink not refused" "$tmp/enforce-symlink.log" "refusing to load"

# enforce: rejects a modified page.
run enforce "$tmp/tampered.dylib" "$tmp/enforce-tampered.log"
expect "enforce: tampered page rejected" "$tmp/enforce-tampered.log" "failed verification"
expect "enforce: tampered loader refuses" "$tmp/enforce-tampered.log" "refusing to load"

# fail closed on an unknown policy.
run bogus "$tmp/signed.dylib" "$tmp/bogus.log"
expect "unknown policy fails closed" "$tmp/bogus.log" "unknown policy"

# The patches vendor copies of the parser; they must match the canonical tool.
if cmp -s "$repo/tools/dc-codesign/dc_codesign.c" "$repo/build/darling-src/src/startup/mldr/dc_codesign.c" \
   && cmp -s "$repo/tools/dc-codesign/dc_codesign.h" "$repo/build/darling-src/src/startup/mldr/dc_codesign.h" \
   && cmp -s "$repo/tools/dc-codesign/dc_codesign.c" "$repo/build/darling-src/src/external/darlingserver/src/dc_codesign.c" \
   && cmp -s "$repo/tools/dc-codesign/dc_codesign.h" "$repo/build/darling-src/src/external/darlingserver/src/dc_codesign.h"; then
	ok "vendored parser matches tools/dc-codesign"
else
	bad "vendored parser drifted from tools/dc-codesign"
fi

printf -- '----\n'

printf 'pass=%d fail=%d skip=%d\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
