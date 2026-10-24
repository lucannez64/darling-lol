#!/bin/sh
# M4 acceptance test: dlopen signature verification, library validation and
# hardened-runtime DYLD_* handling.
#
# Requires a working Darling installation, build SDK headers, clang,
# rcodesign and a self-signed certificate generator (rcodesign provides it).

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
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

for tool in "$RCODESIGN" "$DARLING"; do
	if [ -z "$tool" ] || [ ! -x "$tool" ]; then
		miss "required tool not found: ${tool:-darling}"
		finish
		exit $?
	fi
done
if ! command -v clang >/dev/null 2>&1; then
	miss "clang not found"
	finish
	exit $?
fi
if [ ! -f "$SDK/usr/include/stdio.h" ]; then
	miss "no SDK headers under $SDK"
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

build() { # source output extra...
	src=$1; out=$2; shift 2
	clang -target x86_64-apple-macosx11.0 -fuse-ld=lld -nostdlib -isysroot "$SDK" \
	    "$src" "$@" -lSystem -o "$out"
}

main_src=$repo/tests/security/test_lv_runtime.c
target_c=$repo/tests/security/test_lv_target.c
insert_c=$repo/tests/security/test_lv_insert.c
main_unsigned=$tmp/main-unsigned
main_plain=$tmp/main-plain
main_hard=$tmp/main-hard
main_nolv=$tmp/main-nolv
main_allowenv=$tmp/main-allowenv
main_team=$tmp/main-team
main_clear=$tmp/main-clear
target_unsigned=$tmp/libtarget-unsigned.dylib
target_adhoc=$tmp/libtarget-adhoc.dylib
target_team_a=$tmp/libtarget-team-a.dylib
target_team_b=$tmp/libtarget-team-b.dylib
insert=$tmp/libinsert.dylib

if ! build "$main_src" "$main_unsigned"; then
	miss "failed to build the dlopen test"
	finish
	exit $?
fi
if ! build "$target_c" "$target_unsigned" -dynamiclib; then
	miss "failed to build the target dylib"
	finish
	exit $?
fi
if ! build "$insert_c" "$insert" -dynamiclib; then
	miss "failed to build the inserted dylib"
	finish
	exit $?
fi

cp "$main_unsigned" "$main_plain"
cp "$main_unsigned" "$main_hard"
cp "$main_unsigned" "$main_nolv"
cp "$main_unsigned" "$main_allowenv"
cp "$main_unsigned" "$main_team"
cp "$main_unsigned" "$main_clear"
cp "$target_unsigned" "$target_adhoc"
cp "$target_unsigned" "$target_team_a"
cp "$target_unsigned" "$target_team_b"

disable_lv=$tmp/disable-lv.plist
allow_env=$tmp/allow-env.plist
clear_lv=$tmp/clear-lv.plist
cat >"$disable_lv" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.cs.disable-library-validation</key><true/>
</dict></plist>
EOF
cat >"$allow_env" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
</dict></plist>
EOF
cat >"$clear_lv" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.private.security.clear-library-validation</key><true/>
</dict></plist>
EOF

sign() { # label output extra...
	"$RCODESIGN" sign "$@" >/dev/null 2>&1
}

if ! sign --binary-identifier lv-main-plain "$main_plain"; then
	miss "failed to sign the plain main"
	finish
	exit $?
fi
if ! sign --binary-identifier lv-main-hard --code-signature-flags runtime --code-signature-flags library "$main_hard"; then
	miss "failed to sign the hardened main"
	finish
	exit $?
fi
if ! sign --binary-identifier lv-main-nolv --code-signature-flags runtime --code-signature-flags library --entitlements-xml-file "$disable_lv" "$main_nolv"; then
	miss "failed to sign the disable-library-validation main"
	finish
	exit $?
fi
if ! sign --binary-identifier lv-main-allowenv --code-signature-flags runtime --code-signature-flags library --entitlements-xml-file "$allow_env" "$main_allowenv"; then
	miss "failed to sign the allow-dyld-env main"
	finish
	exit $?
fi
if ! sign --binary-identifier lv-target-adhoc "$target_adhoc"; then
	miss "failed to sign the ad-hoc target"
	finish
	exit $?
fi
if ! sign --binary-identifier lv-insert "$insert"; then
	miss "failed to sign the inserted dylib"
	finish
	exit $?
fi

certs=$tmp/certs
mkdir -p "$certs"
if ! "$RCODESIGN" generate-self-signed-certificate --person-name "Darling LV A" \
    --pem-filename "$certs/team-a" >/dev/null 2>&1; then
	miss "rcodesign could not generate a self-signed certificate"
	finish
	exit $?
fi
if ! "$RCODESIGN" generate-self-signed-certificate --person-name "Darling LV B" \
    --pem-filename "$certs/team-b" >/dev/null 2>&1; then
	miss "rcodesign could not generate the second self-signed certificate"
	finish
	exit $?
fi
if ! sign --pem-source "$certs/team-a.crt" --pem-source "$certs/team-a.key" \
    --team-name TEAMA --binary-identifier lv-main-team \
    --code-signature-flags runtime --code-signature-flags library "$main_team"; then
	miss "failed to sign the TEAMA main"
	finish
	exit $?
fi
if ! sign --pem-source "$certs/team-a.crt" --pem-source "$certs/team-a.key" \
    --team-name TEAMA --binary-identifier lv-target-team-a "$target_team_a"; then
	miss "failed to sign the TEAMA target"
	finish
	exit $?
fi
if ! sign --pem-source "$certs/team-b.crt" --pem-source "$certs/team-b.key" \
    --team-name TEAMB --binary-identifier lv-target-team-b "$target_team_b"; then
	miss "failed to sign the TEAMB target"
	finish
	exit $?
fi
if ! sign --pem-source "$certs/team-a.crt" --pem-source "$certs/team-a.key" \
    --team-name TEAMA --binary-identifier lv-main-clear \
    --code-signature-flags runtime --code-signature-flags library \
    --entitlements-xml-file "$clear_lv" "$main_clear"; then
	miss "failed to sign the clear-library-validation main"
	finish
	exit $?
fi

guest_root=/Volumes/SystemRoot
run_guest() { # output args...
	outfile=$tmp/run.out
	DARLING_CODESIGN_ENFORCE=enforce timeout 120 "$DARLING" shell "$@" >"$outfile" 2>&1
	cat "$outfile"
}

expect() { # label output needle
	case "$2" in
	*"$3"*) ok "$1" ;;
	*) bad "$1 ($2)" ;;
	esac
}
reject() { # label output needle
	case "$2" in
	*"$3"*) bad "$1 (unexpected: $3)" ;;
	*) ok "$1" ;;
	esac
}

out=$(run_guest "$guest_root$main_plain" "$guest_root$target_unsigned")
expect "plain process loads an unsigned dylib" "$out" "DLOPEN_OK"

out=$(run_guest "$guest_root$main_hard" "$guest_root$target_unsigned")
expect "hardened process refuses an unsigned dylib" "$out" "DLOPEN_FAIL"
expect "refusal names library validation" "$out" "Library Validation"

out=$(run_guest "$guest_root$main_hard" "$guest_root$target_adhoc")
expect "hardened process loads an ad-hoc dylib" "$out" "DLOPEN_OK"

out=$(run_guest "$guest_root$main_nolv" "$guest_root$target_unsigned")
expect "disable-library-validation loads an unsigned dylib" "$out" "DLOPEN_OK"

out=$(run_guest "$guest_root$main_team" "$guest_root$target_team_a")
expect "same-team dylib loads" "$out" "DLOPEN_OK"

out=$(run_guest "$guest_root$main_team" "$guest_root$target_team_b")
expect "different-team dylib is refused" "$out" "DLOPEN_FAIL"
expect "different-team refusal names the team ID" "$out" "different team ID"

out=$(run_guest "$guest_root$main_clear" --clear-lv "$guest_root$target_team_b")
expect "clear-library-validation clears the effective LV policy" "$out" "CLEAR_LV_OK"
expect "cleared-LV process loads a different-team dylib" "$out" "DLOPEN_OK"

out=$(run_guest env "DYLD_INSERT_LIBRARIES=$guest_root$insert" "$guest_root$main_hard" "$guest_root$target_adhoc")
reject "hardened process ignores DYLD_INSERT_LIBRARIES" "$out" "INSERTED"
expect "hardened process still loads its explicit dylib" "$out" "DLOPEN_OK"

out=$(run_guest env "DYLD_INSERT_LIBRARIES=$guest_root$insert" "$guest_root$main_allowenv" "$guest_root$target_adhoc")
expect "allow-dyld-environment-variables honors DYLD_INSERT_LIBRARIES" "$out" "INSERTED"
expect "entitled process loads its explicit dylib" "$out" "DLOPEN_OK"

finish
