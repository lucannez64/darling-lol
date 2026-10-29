#!/bin/sh
# Cross-checks dc-codesign against rcodesign (apple-codesign).
#
# Usage:
#   ./test.sh [macho-input]
#   DC_TEST_INPUT=/path/to/file.dylib ./test.sh
#
# Without an explicit input, the script looks for a Mach-O in the Darling build
# tree and skips (exit 0) when none is available.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi

pass=0
fail=0
skip=0

ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail + 1)); }
miss() { printf 'SKIP  %s\n' "$1"; skip=$((skip + 1)); }

if [ ! -x "$here/dc-codesign" ]; then
	echo "test.sh: building dc-codesign" >&2
	make -C "$here" >/dev/null || exit 2
fi

if [ -z "$RCODESIGN" ]; then
	echo "test.sh: rcodesign not found; install apple-codesign or set RCODESIGN" >&2
	exit 2
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

input=${1:-${DC_TEST_INPUT:-}}
if [ -z "$input" ]; then
	input=$(find "$repo/build/darling-src/build" -type f -name '*.dylib' 2>/dev/null | while IFS= read -r f; do
		if file -b "$f" 2>/dev/null | grep -q 'Mach-O'; then
			printf '%s\n' "$f"
			break
		fi
	done)
fi
if [ -z "$input" ] && command -v python3 >/dev/null 2>&1; then
	input="$tmp/minimal.dylib"
	python3 "$here/minimal_macho.py" "$input" || input=""
fi
if [ -z "$input" ] || [ ! -f "$input" ]; then
	miss "no Mach-O input (pass one as \$1, set DC_TEST_INPUT, or install python3)"
	echo "----"
	echo "pass=$pass fail=$fail skip=$skip"
	exit 0
fi

field() { sed -n "s/^$2=//p" "$1"; }

cdhash_ok() {
	# $1 = file, $2 = label, $3 = expected cdhash
	got=$("$here/dc-codesign" cdhash "$1" 2>/dev/null)
	if [ "$got" = "$3" ]; then ok "$2"; else bad "$2 (got '$got' want '$3')"; fi
}

# --- ad-hoc ---------------------------------------------------------------
cp "$input" "$tmp/adhoc.dylib"
if "$RCODESIGN" sign --digest sha256 --binary-identifier com.example.adhoc "$tmp/adhoc.dylib" >/dev/null 2>&1; then
	"$here/dc-codesign" info "$tmp/adhoc.dylib" > "$tmp/adhoc.info" 2>"$tmp/adhoc.err" \
		&& ok "ad-hoc: parse" || bad "ad-hoc: parse ($(cat "$tmp/adhoc.err"))"

	ref=$("$RCODESIGN" print-signature-info "$tmp/adhoc.dylib" 2>/dev/null)
	# Old deployment targets add a SHA-1 primary even with explicit SHA-256;
	# rcodesign prints the requested SHA-256 alternate after that primary.
	ref_sha1=$(printf '%s\n' "$ref" | awk '/slot:/{f=($0 ~ /CodeDirectory/)} f&&/sha1:/{v=$2} END{print v}')
	ref_sha256=$(printf '%s\n' "$ref" | awk '/slot:/{f=($0 ~ /CodeDirectory/)} f&&/sha256:/{v=$2} END{print v}')
	our_sha1=$(field "$tmp/adhoc.info" cd.sha1)
	our_sha256=$(field "$tmp/adhoc.info" cd.sha256)

	if [ -n "$ref_sha1" ] && [ "$our_sha1" = "$ref_sha1" ]; then
		ok "ad-hoc: SHA-1 of CodeDirectory matches rcodesign"
	else
		bad "ad-hoc: SHA-1 mismatch (ours '$our_sha1' ref '$ref_sha1')"
	fi
	if [ -n "$ref_sha256" ] && [ "$our_sha256" = "$ref_sha256" ]; then
		ok "ad-hoc: SHA-256 of CodeDirectory matches rcodesign"
	else
		bad "ad-hoc: SHA-256 mismatch (ours '$our_sha256' ref '$ref_sha256')"
	fi

	htype=$(field "$tmp/adhoc.info" cd.hash_type)
	if [ "$htype" = "1" ]; then
		cdhash_ok "$tmp/adhoc.dylib" "ad-hoc: effective cdhash" "$ref_sha1"
	else
		cdhash_ok "$tmp/adhoc.dylib" "ad-hoc: effective cdhash" "$(printf '%s' "$ref_sha256" | cut -c1-40)"
	fi

	if "$here/dc-codesign" verify "$tmp/adhoc.dylib" >/dev/null 2>&1; then
		ok "ad-hoc: page hashes verify"
	else
		bad "ad-hoc: page hashes verify"
	fi

	# --- tamper --------------------------------------------------------
	cp "$tmp/adhoc.dylib" "$tmp/tampered.dylib"
	printf '\377' | dd of="$tmp/tampered.dylib" bs=1 seek=100 conv=notrunc status=none 2>/dev/null
	if "$here/dc-codesign" verify "$tmp/tampered.dylib" >"$tmp/tamper.out" 2>&1; then
		bad "tampered page is rejected"
	else
		ok "tampered page is rejected"
	fi
else
	miss "ad-hoc signing (rcodesign failed)"
fi

# --- mixed CodeDirectories: macOS chooses SHA-256 over primary SHA-1 --------
cp "$input" "$tmp/multi.dylib"
if "$RCODESIGN" sign --digest sha1 --digest sha256 "$tmp/multi.dylib" >"$tmp/multi-sign.out" 2>&1; then
    "$RCODESIGN" print-signature-info "$tmp/multi.dylib" >"$tmp/multi.ref"
    ref=$(awk '/slot:.*CodeDirectory Alternate #0/{f=1;next} f&&/sha256:/{print $2;exit}' "$tmp/multi.ref")
    cdhash_ok "$tmp/multi.dylib" "mixed SHA-1/SHA-256: strongest cdhash" "$(printf '%s' "$ref" | cut -c1-40)"
    "$here/dc-codesign" info "$tmp/multi.dylib" >"$tmp/multi.info"
    if [ "$(field "$tmp/multi.info" cd.hash_type)" = 2 ] &&
        "$here/dc-codesign" verify "$tmp/multi.dylib" >/dev/null 2>&1; then
        ok "mixed SHA-1/SHA-256: selected page hashes verify"
    else
        bad "mixed SHA-1/SHA-256: selected page hashes verify"
    fi
    # Even the unselected primary must be structurally valid.
    cp "$tmp/multi.dylib" "$tmp/multi-bad.dylib"
    python3 - "$tmp/multi-bad.dylib" <<'PYFIX'
import struct, sys
p = sys.argv[1]
data = bytearray(open(p, 'rb').read())
ncmds = struct.unpack_from('<I', data, 16)[0]
command = 32
for _ in range(ncmds):
    cmd, size = struct.unpack_from('<II', data, command)
    if cmd == 0x1d:
        signature = struct.unpack_from('<I', data, command + 8)[0]
        primary = struct.unpack_from('>I', data, signature + 16)[0]
        data[signature + primary] ^= 1
        break
    command += size
else:
    raise SystemExit('no signature command')
open(p, 'wb').write(data)
PYFIX
    if "$here/dc-codesign" info "$tmp/multi-bad.dylib" >/dev/null 2>&1; then
        bad "malformed unselected CodeDirectory is rejected"
    else
        ok "malformed unselected CodeDirectory is rejected"
    fi
else
    bad "mixed CodeDirectory signing ($(cat "$tmp/multi-sign.out"))"
fi

# --- entitlements ---------------------------------------------------------
cat > "$tmp/ents.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>com.apple.security.cs.allow-jit</key><true/></dict></plist>
EOF
cp "$input" "$tmp/ents.dylib"
if "$RCODESIGN" sign --entitlements-xml-file "$tmp/ents.plist" "$tmp/ents.dylib" >/dev/null 2>&1; then
	out=$("$here/dc-codesign" entitlements "$tmp/ents.dylib" 2>/dev/null)
	case "$out" in
	*"com.apple.security.cs.allow-jit"*) ok "entitlements round-trip" ;;
	*) bad "entitlements round-trip" ;;
	esac

	# The special slot must bind the entitlement bytes to the CodeDirectory.
	cp "$tmp/ents.dylib" "$tmp/ents-tampered.dylib"
	if python3 - "$tmp/ents-tampered.dylib" <<'PY'
import sys
path = sys.argv[1]
data = bytearray(open(path, 'rb').read())
index = data.find(b'allow-jit')
if index < 0:
    raise SystemExit(1)
data[index] ^= 1
open(path, 'wb').write(data)
PY
	then
		if "$here/dc-codesign" verify "$tmp/ents-tampered.dylib" >/dev/null 2>&1; then
			bad "tampered entitlements are rejected"
		else
			ok "tampered entitlements are rejected"
		fi
	else
		miss "could not build the tampered-entitlements fixture"
	fi
else
	miss "entitlements signing (rcodesign failed)"
fi

# --- self-signed identity -------------------------------------------------
if "$RCODESIGN" generate-self-signed-certificate --person-name "Darling Test" \
		--pem-filename "$tmp/cert" >/dev/null 2>&1; then
	cp "$input" "$tmp/devid.dylib"
	if "$RCODESIGN" sign --pem-source "$tmp/cert.crt" --pem-source "$tmp/cert.key" \
			--team-name DARLINGTEST "$tmp/devid.dylib" >/dev/null 2>&1; then
		team=$("$here/dc-codesign" info "$tmp/devid.dylib" 2>/dev/null | sed -n 's/^cd.teamid=//p')
		if [ "$team" = "DARLINGTEST" ]; then
			ok "self-signed: team id parsed"
		else
			bad "self-signed: team id (got '$team')"
		fi
		if "$here/dc-codesign" verify "$tmp/devid.dylib" >/dev/null 2>&1; then
			ok "self-signed: page hashes verify"
		else
			bad "self-signed: page hashes verify"
		fi
	else
		miss "self-signed signing (rcodesign failed)"
	fi
else
	miss "self-signed certificate generation"
fi

# --- negative cases -------------------------------------------------------
head -c 64 "$input" > "$tmp/short.dylib"
if "$here/dc-codesign" info "$tmp/short.dylib" >/dev/null 2>&1; then
	bad "truncated Mach-O rejected"
else
	ok "truncated Mach-O rejected"
fi

printf 'not a mach-o at all' > "$tmp/text.bin"
if "$here/dc-codesign" info "$tmp/text.bin" >/dev/null 2>&1; then
	bad "non-Mach-O rejected"
else
	ok "non-Mach-O rejected"
fi

printf -- '----\n'
printf 'pass=%d fail=%d skip=%d\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
