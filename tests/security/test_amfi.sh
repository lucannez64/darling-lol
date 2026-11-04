#!/bin/sh
# M8 acceptance: unknown AMFI calls fail closed; a non-entitled process cannot
# attach; server-side task-port policy rejects a direct RPC; an entitled
# process can obtain a task port through that same RPC.

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
finish() { printf -- '----\n'; printf 'pass=%d fail=%d skip=%d\n' "$pass" "$fail" "$skip"; [ "$fail" -eq 0 ]; }

for tool in "$RCODESIGN" "$DARLING"; do
	if [ -z "$tool" ] || [ ! -x "$tool" ]; then
		miss "required tool not found: ${tool:-darling}"
		finish
		exit $?
	fi
done
if ! command -v clang >/dev/null 2>&1 || [ ! -f "$SDK/usr/include/stdio.h" ]; then
	miss "clang or SDK headers not found"
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

probe=$tmp/test_amfi
if ! clang -target x86_64-apple-macosx11.0 -DPLATFORM_MacOSX -D_DARWIN_C_SOURCE \
    -fuse-ld=lld -nostdlib -isysroot "$SDK" "$here/test_amfi.c" -lSystem -o "$probe"; then
	miss "failed to build the AMFI probe"
	finish
	exit $?
fi

negative=$tmp/amfi-negative
positive=$tmp/amfi-positive
cp "$probe" "$negative"
cp "$probe" "$positive"
cat >"$tmp/entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>com.apple.security.get-task-allow</key><true/></dict></plist>
PLIST
if ! "$RCODESIGN" sign --binary-identifier amfi-negative "$negative" >/dev/null 2>&1; then
	miss "failed to sign the non-entitled AMFI probe"
	finish
	exit $?
fi
if ! "$RCODESIGN" sign --binary-identifier amfi-positive \
    --entitlements-xml-file "$tmp/entitlements.plist" "$positive" >/dev/null 2>&1; then
	miss "failed to sign the entitled AMFI probe"
	finish
	exit $?
fi

guest_root=/Volumes/SystemRoot
outfile=$tmp/out
run() { # binary [arg]
	bin=$1; shift
	DARLING_CODESIGN_ENFORCE=enforce timeout 120 "$DARLING" shell \
	    "$guest_root$bin" "$@" >"$outfile" 2>&1
	cat "$outfile"
}

out=$(run "$negative")
case "$out" in
*"AMFI_OK"*) ok "unknown AMFI call is ENOTSUP and non-entitled ptrace is EPERM" ;;
*) bad "M8 negative probe failed: $out" ;;
esac

out=$(run "$negative" direct-deny)
case "$out" in
*"DIRECT_DENY"*) ok "direct task_for_pid RPC is denied without get-task-allow" ;;
*) bad "direct task_for_pid denial probe failed: $out" ;;
esac

out=$(run "$positive" direct)
case "$out" in
*"DIRECT_ALLOW"*) ok "direct task_for_pid RPC succeeds for an entitled target" ;;
*) bad "entitled direct task_for_pid probe failed: $out" ;;
esac

out=$(run "$negative" direct-name-deny)
case "$out" in
*"DIRECT_DENY"*) ok "direct task_name_for_pid RPC is denied across uids" ;;
*) bad "direct task_name_for_pid denial probe failed: $out" ;;
esac

out=$(run "$positive" direct-name)
case "$out" in
*"DIRECT_ALLOW"*) ok "direct task_name_for_pid RPC is allowed for an entitled target" ;;
*) bad "entitled direct task_name_for_pid probe failed: $out" ;;
esac

out=$(run "$negative" write-deny)
case "$out" in
*"WRITE_DENY"*) ok "mach_vm_write/process_vm_writev is denied without get-task-allow" ;;
*) bad "mach_vm_write denial probe failed: $out" ;;
esac

out=$(run "$positive" write)
case "$out" in
*"WRITE_ALLOW"*) ok "mach_vm_write/process_vm_writev succeeds for an entitled target" ;;
*) bad "entitled mach_vm_write probe failed: $out" ;;
esac

finish
