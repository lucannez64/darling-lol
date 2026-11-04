#!/bin/sh
# M10 preparation: run the local enforce-mode smoke harness and verify the log
# analyzer catches a real signature-failure line. This is not the full match
# soak; that remains a reference-machine gate.

set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
SOAK=${SOAK:-$repo/tools/security-soak.py}
DARLING=${DARLING:-$(command -v darling 2>/dev/null || true)}

pass=0; fail=0; skip=0
ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail+1)); }
miss() { printf 'SKIP  %s\n' "$1"; skip=$((skip+1)); }
finish() { printf -- '----\n'; printf 'pass=%d fail=%d skip=%d\n' "$pass" "$fail" "$skip"; [ "$fail" -eq 0 ]; }

if [ -z "$DARLING" ] || [ ! -x "$DARLING" ]; then
	miss "darling not found"
	finish
	exit $?
fi
if ! command -v python3 >/dev/null 2>&1; then
	miss "python3 not found"
	finish
	exit $?
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if python3 "$SOAK" run --smoke --darling "$DARLING" --out "$tmp/run" >"$tmp/run.out" 2>&1; then
	if python3 - "$tmp/run/soak.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
assert report["enforce"] is True
assert report["failures"] == 0
assert len(report["results"]) >= 3
PY
	then
		ok "enforce smoke harness passes with no security findings"
	else
		bad "enforce smoke report had failures"
	fi
else
	bad "enforce smoke harness failed: $(tail -5 "$tmp/run.out")"
fi

printf 'mldr: refusing to load /tmp/fixture (code signing)\n' >"$tmp/bad.log"
if python3 "$SOAK" analyze "$tmp/bad.log" >"$tmp/analyze.out" 2>&1; then
	bad "log analyzer accepted a signature failure"
else
	if grep -q 'security_findings=1' "$tmp/analyze.out"; then
		ok "log analyzer rejects a signature-failure line"
	else
		bad "log analyzer did not report the expected finding"
	fi
fi

finish
