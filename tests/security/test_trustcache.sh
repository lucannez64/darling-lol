#!/bin/sh
# M6 acceptance: local Ed25519 trust store, add/remove, tamper and rollback.
# Runs as root for the system store; the rootless CLI path is exercised too.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi
DARLING=${DARLING:-$(command -v darling 2>/dev/null || true)}
DCC=${DCC:-$repo/tools/dc-codesign/dc-codesign}
TC=${TC:-$repo/tools/dc-trustcache.py}
SIGNER=${SIGNER:-$repo/tools/sign-darling-runtime.sh}
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

if [ "$(id -u)" -ne 0 ]; then
	miss "trust-store acceptance requires root"
	finish
	exit $?
fi
for tool in "$RCODESIGN" "$DARLING" "$DCC" "$TC" "$SIGNER"; do
	if [ -z "$tool" ] || [ ! -x "$tool" ]; then
		miss "required tool not found: ${tool:-unknown}"
		finish
		exit $?
	fi
done
if ! command -v clang >/dev/null 2>&1 || [ ! -f "$SDK/usr/include/stdio.h" ]; then
	miss "clang or SDK headers not found"
	finish
	exit $?
fi

store=$("$TC" default-store) || { miss "could not determine trust-store path"; finish; exit $?; }
trust_dir=${store%/*}
case "$store" in
"$prefix"/*) bad "trust store is inside the mutable prefix: $store" ;;
*) ok "trust store lives outside the writable prefix" ;;
esac

tmp=$(mktemp -d)
backup=
if [ -e "$trust_dir" ]; then
	backup=$tmp/trust-backup
	if ! cp -a "$trust_dir" "$backup"; then
		echo "test_trustcache: could not back up $trust_dir; aborting" >&2
		rm -rf "$tmp"
		exit 1
	fi
fi
started=0
cleaned=0
if ! pgrep -x darlingserver >/dev/null 2>&1; then
	started=1
fi
cleanup() {
	[ "$cleaned" -eq 0 ] || return 0
	cleaned=1
	if [ "$started" -eq 1 ]; then
		timeout 30 "$DARLING" shutdown >/dev/null 2>&1 || true
	fi
	rm -rf "$trust_dir"
	if [ -n "$backup" ]; then
		mkdir -p "$(dirname "$trust_dir")"
		if ! cp -a "$backup" "$trust_dir"; then
			echo "test_trustcache: could not restore $trust_dir; backup kept at $backup" >&2
			return 0
		fi
	fi
	rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# Start from a stopped namespace so the server's rollback cache is
# deterministic.
timeout 30 "$DARLING" shutdown >/dev/null 2>&1 || true
started=1
sleep 1

# The installed runtime must be in the store before removing the M4 path
# exception. Populate it once if the environment has not done so.
if [ ! -f "$store" ]; then
	if ! "$SIGNER" --sdk --quiet >/dev/null 2>&1; then
		miss "runtime trust store is missing and the signer could not populate it"
		finish
		exit $?
	fi
fi
if ! "$TC" verify "$store" >/dev/null 2>&1; then
	miss "runtime trust store does not verify"
	finish
	exit $?
fi

build() { # source output extra...
	src=$1; out=$2; shift 2
	clang -target x86_64-apple-macosx11.0 -DPLATFORM_MacOSX -D_DARWIN_C_SOURCE \
	    -fuse-ld=lld -nostdlib -isysroot "$SDK" "$src" "$@" -lSystem -o "$out"
}

main=$tmp/main
target=$tmp/target.dylib
if ! build "$here/test_lv_runtime.c" "$main" || \
   ! build "$here/test_lv_target.c" "$target" -dynamiclib; then
	miss "failed to build the trust-store fixtures"
	finish
	exit $?
fi
if ! "$RCODESIGN" generate-self-signed-certificate --person-name "Darling TC" \
    --pem-filename "$tmp/cert" >/dev/null 2>&1; then
	miss "could not generate a self-signed certificate"
	finish
	exit $?
fi
if ! "$RCODESIGN" sign --pem-source "$tmp/cert.crt" --pem-source "$tmp/cert.key" \
    --team-name TEAMA --binary-identifier tc-main \
    --code-signature-flags runtime --code-signature-flags library "$main" >/dev/null 2>&1 || \
   ! "$RCODESIGN" sign --pem-source "$tmp/cert.crt" --pem-source "$tmp/cert.key" \
    --team-name TEAMB --binary-identifier tc-target "$target" >/dev/null 2>&1; then
	miss "failed to sign the trust-store fixtures"
	finish
	exit $?
fi

target_hash=$("$DCC" cdhash "$target") || {
	miss "dc-codesign could not read the target cdhash"
	finish
	exit $?
}

guest_root=/Volumes/SystemRoot
run() {
	outfile=$tmp/out
	# Restart the namespace so the server sees the latest store file and
	# exercises the persisted generation anchor.
	timeout 30 "$DARLING" shutdown >/dev/null 2>&1 || true
	sleep 1
	DARLING_CODESIGN_ENFORCE=enforce timeout 120 "$DARLING" shell \
	    "$guest_root$main" "$guest_root$target" >"$outfile" 2>&1
	cat "$outfile"
}

"$TC" add "$store" "$target_hash" >/dev/null || { miss "trust-store add failed"; finish; exit $?; }
cp "$store" "$tmp/gen2"

out=$(run)
case "$out" in
*"DLOPEN_OK"*) ok "signed entry allows a different-team dylib" ;;
*) bad "entry was not honored: $out" ;;
esac

"$TC" add "$store" 0000000000000000000000000000000000000000 >/dev/null 2>&1 || true
cp "$store" "$tmp/gen3"
out=$(run)
case "$out" in
*"DLOPEN_OK"*) ok "higher-generation store is still accepted" ;;
*) bad "higher-generation store was rejected: $out" ;;
esac

cp "$tmp/gen2" "$store"
out=$(run)
case "$out" in
*"different team ID"*) ok "generation rollback is rejected" ;;
*) bad "generation rollback was accepted: $out" ;;
esac

# Tamper a valid generation-3 entry byte.
cp "$tmp/gen3" "$store"
python3 - "$store" <<'PY'
import sys
path = sys.argv[1]
data = bytearray(open(path, 'rb').read())
if len(data) < 53:
    raise SystemExit(1)
data[52] ^= 1
open(path, 'wb').write(data)
PY
if "$TC" verify "$store" >/dev/null 2>&1; then
	bad "tampered trust store verifies"
else
	ok "tampered trust store is rejected by the CLI"
fi
out=$(run)
case "$out" in
*"different team ID"*) ok "tampered trust store is ignored by the server" ;;
*) bad "tampered trust store was accepted: $out" ;;
esac

# Remove the signature entirely.
cp "$tmp/gen3" "$store"
python3 - "$store" <<'PY'
import sys
path = sys.argv[1]
data = open(path, 'rb').read()
open(path, 'wb').write(data[:-68])
PY
if "$TC" verify "$store" >/dev/null 2>&1; then
	bad "store without a signature verifies"
else
	ok "store without a signature fails closed"
fi

# Rootless-style CLI selection and key creation. This does not spin up a
# second user-namespace server; it verifies the default config path and that
# an unprivileged owner can initialize a key/store outside the prefix.
if command -v setpriv >/dev/null 2>&1; then
	rootless_home=$tmp/rootless-home
	rootless_config=$tmp/rootless-config
	chmod 755 "$tmp"
	mkdir -p "$rootless_home" "$rootless_config"
	chown 1000:1000 "$rootless_home" "$rootless_config"
	rootless_store=$(setpriv --reuid=1000 --regid=1000 --clear-groups \
	    env -u DARLING_TRUST_DIR HOME="$rootless_home" XDG_CONFIG_HOME="$rootless_config" \
	    python3 "$TC" default-store 2>/dev/null)
	case "$rootless_store" in
	"$rootless_config/darling/trustcache")
		ok "rootless CLI selects \$XDG_CONFIG_HOME/darling/trustcache" ;;
	*) bad "unexpected rootless store: ${rootless_store:-<empty>}" ;;
	esac
	if setpriv --reuid=1000 --regid=1000 --clear-groups \
	    env -u DARLING_TRUST_DIR HOME="$rootless_home" XDG_CONFIG_HOME="$rootless_config" \
	    python3 "$TC" init "$rootless_store" >/dev/null 2>&1 &&
	   setpriv --reuid=1000 --regid=1000 --clear-groups \
	    env -u DARLING_TRUST_DIR HOME="$rootless_home" XDG_CONFIG_HOME="$rootless_config" \
	    python3 "$TC" verify "$rootless_store" >/dev/null 2>&1; then
		ok "rootless store initializes and verifies under its owner"
	else
		bad "rootless store could not be initialized"
	fi
else
	miss "setpriv not available for the rootless CLI check"
fi

finish
