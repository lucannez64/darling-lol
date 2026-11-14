#!/bin/sh
# Install the Metal wrapper and Indium overlays built by build-riot-metal-runtime.py / build-riot-indium-runtime.py
# as a pair (the Indium blit interface they share changed), with a backup of the files they replace.
# RIOT_METAL_ARTIFACT / RIOT_INDIUM_ARTIFACT select other builds. --rollback puts the backed-up pair back. Neither stops a process: exit the game and both clients, then
# `darling shutdown` and `sh scripts/run.sh` to activate.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$project_dir/tools/runtime-paths.sh"
metal="$guest_home/RuntimeFrameworks/Metal.framework/Versions/A/Metal"
indium="$guest_home/RuntimeLibraries/libindium.dylib"
backups="$project_dir/build/metal-pair-backups"
if [ "${1:-}" = "--rollback" ]; then
	source_metal="$backups/Metal"
	source_indium="$backups/libindium.dylib"
else
	source_metal=${RIOT_METAL_ARTIFACT:-$project_dir/build/riot-Metal}
	source_indium=${RIOT_INDIUM_ARTIFACT:-$project_dir/build/riot-libindium.dylib}
	mkdir -p "$backups"
	# a repeated install must not replace the backup with the pair it installed last time
	cmp -s "$metal" "$source_metal" || cp -p "$metal" "$backups/Metal"
	cmp -s "$indium" "$source_indium" || cp -p "$indium" "$backups/libindium.dylib"
fi
for file in "$source_metal" "$source_indium"; do
	[ -f "$file" ] || { echo "missing $file" >&2; exit 1; }
done
install -m755 "$source_indium" "$indium.next" && mv -f "$indium.next" "$indium"
install -m755 "$source_metal" "$metal.next" && mv -f "$metal.next" "$metal"
sha256sum "$indium" "$metal"
