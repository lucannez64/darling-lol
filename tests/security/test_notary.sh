#!/bin/sh
# M9 preparation: quarantine xattr, ticket-shaped trailer detection, and CMS
# verification against a local test CA. This does not validate Apple policy and
# must never report notarized=true.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi
NOTARY=${NOTARY:-$repo/tools/dc-notary.py}
CMS=${CMS:-$repo/tools/dc-cms-verify.py}
MINIMAL=${MINIMAL:-$repo/tools/dc-codesign/minimal_macho.py}

pass=0; fail=0; skip=0
ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail+1)); }
miss() { printf 'SKIP  %s\n' "$1"; skip=$((skip+1)); }
finish() { printf -- '----\n'; printf 'pass=%d fail=%d skip=%d\n' "$pass" "$fail" "$skip"; [ "$fail" -eq 0 ]; }

for tool in python3 openssl; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		miss "required tool not found: $tool"
		finish
		exit $?
	fi
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# --- quarantine xattr --------------------------------------------------------
file=$tmp/quarantine
printf 'fixture\n' >"$file"
if [ "$(python3 "$NOTARY" quarantine-get "$file")" = "absent" ]; then
	ok "quarantine starts absent"
else
	bad "quarantine did not start absent"
fi
value='0081;1700000000;Darling;ABCD'
python3 "$NOTARY" quarantine-set "$file" "$value" >/dev/null
if [ "$(python3 "$NOTARY" quarantine-get "$file")" = "$value" ]; then
	ok "quarantine set/get round-trip"
else
	bad "quarantine round-trip failed"
fi
python3 "$NOTARY" quarantine-clear "$file" >/dev/null
if [ "$(python3 "$NOTARY" quarantine-get "$file")" = "absent" ]; then
	ok "quarantine clear"
else
	bad "quarantine clear failed"
fi

# --- ticket-shaped trailer --------------------------------------------------
python3 - "$file" <<'PY'
import struct, sys
with open(sys.argv[1], "ab") as output:
    output.write(b"ticket-shaped-test")
    output.write(struct.pack("<4sHHI4s", b"t8lr", 1, 2, 19, b"\0" * 4))
PY
python3 - "$file" "$NOTARY" <<'PY'
import json, subprocess, sys
path, tool = sys.argv[1], sys.argv[2]
result = json.loads(subprocess.check_output([sys.executable, tool, "ticket-scan", "--json", path]))
assert result["present"] is True
assert result["kind"] == "ticket"
assert result["validated"] is False
assert result["notarized"] is False
PY
if [ $? -eq 0 ]; then
	ok "ticket-shaped trailer is detected but never marked validated/notarized"
else
	bad "ticket trailer detection failed"
fi
python3 "$NOTARY" assess "$file" >"$tmp/assess.json"
python3 - "$tmp/assess.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["notarized"] is False
assert result["notarization_validation"] == "not-run"
assert result["ticket"]["present"] is True
PY
if [ $? -eq 0 ]; then
	ok "assess keeps notarized=false"
else
	bad "assess fabricated a notarization result"
fi

# --- CMS against a local test CA -------------------------------------------
if [ -z "$RCODESIGN" ] || [ ! -x "$RCODESIGN" ]; then
	miss "rcodesign not found; CMS fixture skipped"
	finish
	exit $?
fi
if ! python3 "$MINIMAL" "$tmp/minimal.dylib" >/dev/null 2>&1; then
	miss "could not create a minimal Mach-O"
	finish
	exit $?
fi
mkdir -p "$tmp/ca"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$tmp/ca/ca.key" -out "$tmp/ca/ca.crt" \
	-days 2 -subj '/CN=Darling Test CA' \
	-addext 'basicConstraints=critical,CA:TRUE' \
	-addext 'keyUsage=critical,keyCertSign,cRLSign' >/dev/null 2>&1 || {
	miss "could not create local CA"
	finish
	exit $?
}
openssl req -newkey rsa:2048 -nodes -keyout "$tmp/ca/leaf.key" -out "$tmp/ca/leaf.csr" \
	-subj '/CN=Darling Test Signer' >/dev/null 2>&1
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning\n' >"$tmp/ca/leaf.ext"
openssl x509 -req -in "$tmp/ca/leaf.csr" -CA "$tmp/ca/ca.crt" -CAkey "$tmp/ca/ca.key" \
	-CAcreateserial -out "$tmp/ca/leaf.crt" -days 2 -extfile "$tmp/ca/leaf.ext" >/dev/null 2>&1
cp "$tmp/minimal.dylib" "$tmp/signed.dylib"
if ! "$RCODESIGN" sign --pem-source "$tmp/ca/leaf.crt" --pem-source "$tmp/ca/leaf.key" \
    --team-name LOCAL --binary-identifier local-cms "$tmp/signed.dylib" >/dev/null 2>&1; then
	miss "rcodesign could not sign with the local CA leaf"
	finish
	exit $?
fi
python3 "$CMS" "$tmp/signed.dylib" --ca "$tmp/ca/ca.crt" --json >"$tmp/cms.json"
python3 - "$tmp/cms.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["cms_present"] is True
assert result["signature_valid"] is True
assert result["chain"] == "valid"
assert result["content_binding"] == "unverified"
assert result["notarized"] is False
PY
if [ $? -eq 0 ]; then
	ok "CMS signature verifies against the local CA (content binding unverified)"
else
	bad "local CMS verification failed"
fi
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$tmp/ca/wrong.key" -out "$tmp/ca/wrong.crt" \
	-days 2 -subj '/CN=Wrong CA' >/dev/null 2>&1
if python3 "$CMS" "$tmp/signed.dylib" --ca "$tmp/ca/wrong.crt" --json >/dev/null 2>&1; then
	bad "CMS accepted an unrelated local CA"
else
	ok "CMS rejects an unrelated local CA"
fi

finish
