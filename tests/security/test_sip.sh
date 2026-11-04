#!/bin/sh
# M7 acceptance: SIP config from csrctl, protected-path writes, /usr/local
# exception and symlink/.. escapes. Requires a working Darling prefix.

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
prefix=${DPREFIX:-${HOME:-/root}/.darling}

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
	rm -f "$prefix/usr/local/sip-inode-link"
	rm -f "$prefix/private/tmp/sip-link" "$prefix/usr/local/sip-test" "$prefix/private/etc/sip-test"
	rm -f "$prefix/private/etc/sip-file" "$prefix/private/etc/sip-inode" "$prefix/private/etc/sip-newlink" "$prefix/private/tmp/sip-hardlink" "$prefix/private/tmp/sip-inode-link"
	rmdir "$prefix/private/etc/sip-newdir" 2>/dev/null || true
	rm -rf "$tmp"
	if [ "$started" -eq 1 ]; then
		started=0
		timeout 30 "$DARLING" shutdown >/dev/null 2>&1 || true
	fi
}
trap cleanup EXIT HUP INT TERM

probe=$tmp/test_sip
if ! clang -target x86_64-apple-macosx11.0 -DPLATFORM_MacOSX -D_DARWIN_C_SOURCE \
    -fuse-ld=lld -nostdlib -isysroot "$SDK" "$here/test_sip.c" -lSystem -o "$probe"; then
	miss "failed to build the SIP probe"
	finish
	exit $?
fi
if ! "$RCODESIGN" sign --binary-identifier sip-probe "$probe" >/dev/null 2>&1; then
	miss "failed to sign the SIP probe"
	finish
	exit $?
fi

# Create the symlink inside the guest so its absolute target is resolved by
# Darling's vchroot, not by the host.
if ! timeout 60 "$DARLING" shell /bin/ln -sfn /private/etc /private/tmp/sip-link >/dev/null 2>&1; then
	miss "could not create the guest symlink fixture"
	finish
	exit $?
fi

# Files used by the inode/hardlink and VFS probes. The hardlink is created
# while SIP is off, outside the protected tree, then used by the "vfs" run.
mkdir -p "$prefix/private/etc" "$prefix/private/tmp" "$prefix/usr/local"
printf 'sip\n' >"$prefix/private/etc/sip-file"
printf 'sip\n' >"$prefix/private/etc/sip-inode"
rm -f "$prefix/private/tmp/sip-inode-link"
ln "$prefix/private/etc/sip-inode" "$prefix/private/tmp/sip-inode-link"
rm -f "$prefix/usr/local/sip-inode-link"
ln "$prefix/private/etc/sip-inode" "$prefix/usr/local/sip-inode-link"

guest_root=/Volumes/SystemRoot
outfile=$tmp/out
run() { # policy [extra]
	policy=$1; extra=${2:-}
	DARLING_CODESIGN_ENFORCE=enforce DARLING_SIP_ENFORCE="$policy" \
	    timeout 120 "$DARLING" shell env DARLING_SIP_ENFORCE="$policy" \
	    "$guest_root$probe" $extra >"$outfile" 2>&1
	cat "$outfile"
}

out=$(run off)
case "$out" in
*"csr_config=0x00000fff"*"SIP_OFF_OK"*) ok "SIP off: writes allowed and csrctl reports all allow bits" ;;
*) bad "SIP off probe: $out" ;;
esac

out=$(run on)
case "$out" in
*"csr_config=0x00000000"*"SIP_ON_OK"*) ok "SIP on: protected writes refused, /usr/local and symlink/.. checks" ;;
*) bad "SIP on probe: $out" ;;
esac

out=$(run on vfs)
case "$out" in
*"SIP_VFS_OK"*) ok "SIP on: chmod/chown/utimes/xattr/truncate/link/symlink/rmdir/fd/mmap and hardlink checks" ;;
*) bad "SIP on VFS probe: $out" ;;
esac

finish
