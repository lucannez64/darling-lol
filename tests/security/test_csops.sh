#!/bin/sh
# Guest-side acceptance test for M3 csops: signed, unsigned and entitled
# binaries, cross-process queries and pid reuse (dead pid -> ESRCH).
#
# Requires a working Darling installation (darling shell), the built SDK for
# headers, rcodesign and the dc-codesign CLI.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
DCC=${DCC:-$repo/tools/dc-codesign/dc-codesign}
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi
DARLING=${DARLING:-$(command -v darling 2>/dev/null || true)}
SDK=${DARLING_SDK_HEADERS:-$repo/build/darling-src/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk}
if [ ! -f "$SDK/usr/include/stdio.h" ]; then
	SDK=${DARLING_SDK:-/usr/local/libexec/darling}
fi

pass=0; fail=0; skip=0
ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail+1)); }
miss() { printf 'SKIP  %s\n' "$1"; skip=$((skip+1)); }

finish() {
	printf -- '----\n'
	printf 'pass=%d fail=%d skip=%d\n' "$pass" "$fail" "$skip"
	[ "$fail" -eq 0 ]
}

for tool in "$DCC" "$RCODESIGN" "$DARLING"; do
	if [ -z "$tool" ] || [ ! -x "$tool" ]; then
		miss "required tool not found: ${tool:-darling}"
		finish
		exit $?
	fi
done
if [ ! -f "$SDK/usr/include/stdio.h" ]; then
	miss "no SDK headers under $SDK"
	finish
	exit $?
fi
if ! command -v clang >/dev/null 2>&1; then
	miss "clang not found"
	finish
	exit $?
fi

tmp=$(mktemp -d)
started=0
if ! pgrep -x darlingserver >/dev/null 2>&1; then
	started=1
fi
cleanup() {
	rm -rf "$tmp"
	if [ "$started" -eq 1 ]; then
		started=0
		timeout 30 "$DARLING" shutdown >/dev/null 2>&1 || true
	fi
}
trap cleanup EXIT HUP INT TERM

src=$repo/tests/security/test_csops.c
unsigned=$tmp/test_csops_unsigned
signed=$tmp/test_csops_signed
entitled=$tmp/test_csops_entitled
ents=$tmp/entitlements.plist

if ! clang -target x86_64-apple-macosx11.0 -fuse-ld=lld -nostdlib -isysroot "$SDK" \
    "$src" -lSystem -o "$unsigned" >/dev/null 2>&1; then
	miss "failed to build the guest test with clang"
	finish
	exit $?
fi

cat >"$ents" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>get-task-allow</key>
  <true/>
  <key>com.apple.security.cs.disable-library-validation</key>
  <true/>
</dict>
</plist>
EOF

cp "$unsigned" "$signed"
cp "$unsigned" "$entitled"
if ! "$RCODESIGN" sign --binary-identifier test_csops "$signed" >/dev/null 2>&1; then
	miss "rcodesign failed to sign the test binary"
	finish
	exit $?
fi
if ! "$RCODESIGN" sign --binary-identifier test_csops --entitlements-xml-file "$ents" "$entitled" >/dev/null 2>&1; then
	miss "rcodesign failed to sign the entitled test binary"
	finish
	exit $?
fi

guest_root=/Volumes/SystemRoot
run_guest() { # policy mode path
	# Do not capture the guest's stdout through a pipe: the test forks a
	# child, which inherits the descriptor and can keep command substitution
	# waiting after the parent exits. A regular file has no such reader.
	outfile=$tmp/run.out
	DARLING_CODESIGN_ENFORCE="$1" timeout 120 "$DARLING" shell "$guest_root$3" "$2" >"$outfile" 2>&1
	cat "$outfile"
}

check_ok() { # label output expected_cdhash
	case "$2" in
	*CSOPS_OK*"cdhash=$3"*) ok "$1" ;;
	*CSOPS_OK*) bad "$1 (cdhash mismatch; expected $3)" ;;
	*) bad "$1 ($2)" ;;
	esac
}

signed_cdhash=$("$DCC" cdhash "$signed") || signed_cdhash=
entitled_cdhash=$("$DCC" cdhash "$entitled") || entitled_cdhash=
if [ -z "$signed_cdhash" ] || [ -z "$entitled_cdhash" ]; then
	miss "dc-codesign could not read the fixture cdhashes"
	finish
	exit $?
fi

out=$(run_guest enforce signed "$signed")
check_ok "signed binary: valid flags, blob, identity, cross-process" "$out" "$signed_cdhash"

out=$(run_guest warn unsigned "$unsigned")
case "$out" in
*CSOPS_OK*flags=0x00000000*cdhash=0000000000000000000000000000000000000000*)
	ok "unsigned binary: truthful zero flags and EINVAL getters" ;;
*)
	bad "unsigned binary ($out)" ;;
esac

out=$(run_guest enforce entitlements "$entitled")
check_ok "entitled binary: get-task-allow and entitlement blob" "$out" "$entitled_cdhash"

finish
