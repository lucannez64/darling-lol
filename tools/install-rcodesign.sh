#!/bin/sh
# Make sure rcodesign (apple-codesign) is available for sign-darling-runtime.sh.
#
# Without it the runtime overlays that every Riot process loads stay unsigned
# (issue #31). Looks in PATH, ~/.local/bin and ~/.cargo/bin first; otherwise
# downloads the pinned upstream release, checks its SHA-256 and installs the
# binary to ~/.local/bin/rcodesign. Prints the path it settled on.
#
# Exit: 0 rcodesign available, 1 it could not be installed.

set -eu

VERSION=0.29.0
NAME="apple-codesign-$VERSION-x86_64-unknown-linux-musl"
URL="https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F$VERSION/$NAME.tar.gz"
SHA256=dbe85cedd8ee4217b64e9a0e4c2aef92ab8bcaaa41f20bde99781ff02e600002

home=${HOME:-/nonexistent}
for candidate in "$(command -v rcodesign 2>/dev/null || true)" \
		"$home/.local/bin/rcodesign" "$home/.cargo/bin/rcodesign"; do
	if [ -n "$candidate" ] && [ -x "$candidate" ]; then
		echo "$candidate"
		exit 0
	fi
done

fail() {
	echo "install-rcodesign.sh: $1" >&2
	echo "  Install it yourself, then re-run the update:" >&2
	echo "    cargo install apple-codesign --locked   (or your distro's apple-codesign package)" >&2
	exit 1
}

[ "$(uname -m)" = x86_64 ] || fail "no pinned build for $(uname -m)"
command -v curl >/dev/null 2>&1 || fail "curl is needed to download rcodesign"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is needed to check the download"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

echo "install-rcodesign.sh: downloading apple-codesign $VERSION" >&2
curl -fsSL --retry 2 -o "$tmp/rcodesign.tar.gz" "$URL" || fail "download failed: $URL"
echo "$SHA256  $tmp/rcodesign.tar.gz" | sha256sum -c --status - ||
	fail "checksum mismatch for $URL"
tar -xzf "$tmp/rcodesign.tar.gz" -C "$tmp" "$NAME/rcodesign" || fail "unexpected archive layout"

mkdir -p "$home/.local/bin"
install -m 0755 "$tmp/$NAME/rcodesign" "$home/.local/bin/rcodesign"
"$home/.local/bin/rcodesign" --version >/dev/null 2>&1 || fail "the installed rcodesign does not run"
echo "$home/.local/bin/rcodesign"
