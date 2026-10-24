#!/bin/sh
# Builds a seed corpus from real signatures and runs the parser fuzzer.
#
# Usage:
#   ./fuzz/run.sh [seconds] [macho-input]
#
# Seeds come from a Darwin Mach-O signed with rcodesign (ad-hoc, SHA-1,
# entitlements) plus truncations and random bytes. Requires clang, rcodesign
# and OpenSSL headers.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)
repo=$(CDPATH= cd -- "$root/../.." && pwd)

seconds=${1:-60}
input=${2:-${DC_TEST_INPUT:-}}

CLANG=${CLANG:-clang}
RCODESIGN=${RCODESIGN:-$(command -v rcodesign 2>/dev/null || true)}
if [ -z "$RCODESIGN" ] && [ -x "${HOME:-/nonexistent}/.local/bin/rcodesign" ]; then
	RCODESIGN="$HOME/.local/bin/rcodesign"
fi

command -v "$CLANG" >/dev/null || { echo "fuzz/run.sh: clang not found" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if [ -z "$input" ]; then
	input=$(find "$repo/build/darling-src/build" -type f -name '*.dylib' 2>/dev/null | while IFS= read -r f; do
		if file -b "$f" 2>/dev/null | grep -q 'Mach-O'; then
			printf '%s\n' "$f"
			break
		fi
	done)
fi
if [ -z "$input" ] && command -v python3 >/dev/null 2>&1; then
	input="$work/minimal.dylib"
	python3 "$root/minimal_macho.py" "$input" || input=""
fi
if [ -z "$input" ] || [ ! -f "$input" ]; then
	echo "fuzz/run.sh: no Mach-O input (pass one, set DC_TEST_INPUT, or install python3)" >&2
	exit 2
fi

echo "== building fuzzer =="
"$CLANG" -D_GNU_SOURCE -g -O1 \
	-fsanitize=fuzzer,address,undefined -fno-omit-frame-pointer \
	-o "$root/dc-parse-fuzz" "$here/dc_parse_fuzz.c" "$root/dc_codesign.c" \
	-lcrypto

corpus="$work/corpus"
mkdir -p "$corpus"

echo "== building corpus from $input =="
cp "$input" "$corpus/raw"
head -c 64 "$input" > "$corpus/short" 2>/dev/null || true
head -c 4096 "$input" > "$corpus/head4k" 2>/dev/null || true

if [ -n "$RCODESIGN" ]; then
	cp "$input" "$corpus/adhoc"
	"$RCODESIGN" sign --binary-identifier com.example.fuzz "$corpus/adhoc" >/dev/null 2>&1 || rm -f "$corpus/adhoc"

	cp "$input" "$corpus/sha1"
	"$RCODESIGN" sign --digest sha1 "$corpus/sha1" >/dev/null 2>&1 || rm -f "$corpus/sha1"

	cat > "$corpus/ents.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>com.apple.security.cs.allow-jit</key><true/></dict></plist>
EOF
	cp "$input" "$corpus/ents"
	"$RCODESIGN" sign --entitlements-xml-file "$corpus/ents.plist" "$corpus/ents" >/dev/null 2>&1 || rm -f "$corpus/ents"
	rm -f "$corpus/ents.plist"
else
	echo "fuzz/run.sh: rcodesign not found, corpus limited to the unsigned input" >&2
fi

printf 'seed-%s\n' 1 2 3 > "$corpus/random" 2>/dev/null || true

count=$(find "$corpus" -type f | wc -l)
echo "corpus files: $count"

echo "== fuzzing for ${seconds}s =="
ASAN_OPTIONS=${ASAN_OPTIONS:-detect_leaks=0} \
	"$root/dc-parse-fuzz" -max_total_time="$seconds" -max_len=131072 \
	-print_final_stats=1 "$corpus"
