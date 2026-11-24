#!/bin/sh
# Build the experimental native Wayland transport: the host ELF shim, its smoke test and (without
# --host-only) the Mach-O wrapper. Builds into build/ only; installation is done by the runtime installer.
set -eu
host_only=0
for arg in "$@"; do
  case "$arg" in
    --host-only) host_only=1 ;;
    *) echo "usage: $0 [--host-only]" >&2; exit 2 ;;
  esac
done
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dir="$project_dir/build/darling-src"
out_dir="$project_dir/build"
proto_dir="$out_dir/wayland-protocols"

missing=""
command -v pkg-config >/dev/null 2>&1 || missing="$missing pkg-config"
if [ -z "$missing" ]; then
  for module in wayland-client wayland-egl xkbcommon wayland-protocols egl x11; do
    pkg-config --exists "$module" 2>/dev/null || missing="$missing $module"
  done
fi
command -v wayland-scanner >/dev/null 2>&1 || missing="$missing wayland-scanner"
command -v cc >/dev/null 2>&1 || missing="$missing cc"
if [ -n "$missing" ]; then
  echo "skipping the experimental Wayland transport: missing$missing" >&2
  exit 0
fi

pkgdata=$(pkg-config --variable=pkgdatadir wayland-protocols)
mkdir -p "$proto_dir"

generate() {
  # generate <xml relative to pkgdatadir> <basename>
  [ -f "$pkgdata/$1" ] || return 1
  wayland-scanner client-header "$pkgdata/$1" "$proto_dir/$2-client-protocol.h"
  wayland-scanner private-code "$pkgdata/$1" "$proto_dir/$2-protocol.c"
}
protocols=""
for entry in \
  stable/xdg-shell/xdg-shell.xml:xdg-shell \
  stable/viewporter/viewporter.xml:viewporter \
  staging/xdg-activation/xdg-activation-v1.xml:xdg-activation-v1 \
  unstable/pointer-constraints/pointer-constraints-unstable-v1.xml:pointer-constraints-unstable-v1 \
  unstable/relative-pointer/relative-pointer-unstable-v1.xml:relative-pointer-unstable-v1 \
  unstable/xdg-output/xdg-output-unstable-v1.xml:xdg-output-unstable-v1 \
  unstable/idle-inhibit/idle-inhibit-unstable-v1.xml:idle-inhibit-unstable-v1
do
  if ! generate "${entry%%:*}" "${entry##*:}"; then
    echo "skipping the experimental Wayland transport: wayland-protocols lacks ${entry%%:*}" >&2
    exit 0
  fi
  protocols="$protocols $proto_dir/${entry##*:}-protocol.c"
done
# wp_pointer_warp_v1 is newer (wayland-protocols 1.45); without it pointer_warp only works while locked.
warp_flag=""
if generate staging/pointer-warp/pointer-warp-v1.xml pointer-warp-v1; then
  protocols="$protocols $proto_dir/pointer-warp-v1-protocol.c"
  warp_flag="-DDWL_HAVE_POINTER_WARP"
else
  echo "note: wayland-protocols has no pointer-warp-v1; pointer_warp is limited to locked pointers" >&2
fi

# Presentation timing is optional on older protocol packages and compositors.
presentation_flag=""
if generate stable/presentation-time/presentation-time.xml presentation-time; then
  protocols="$protocols $proto_dir/presentation-time-protocol.c"
  presentation_flag="-DDWL_HAVE_PRESENTATION"
fi

# shellcheck disable=SC2086
cc -std=gnu11 -shared -fPIC -O2 -Wall -Wextra $warp_flag $presentation_flag \
  "-Wl,-soname,$out_dir/libdarling-wayland.so" \
  -I"$proto_dir" -I"$project_dir/tools" \
  "$project_dir/tools/darling-wayland.c" $protocols -o "$out_dir/libdarling-wayland.next.so" \
  $(pkg-config --cflags --libs wayland-client wayland-egl xkbcommon)

cc -std=gnu11 -O2 -Wall -Wextra -I"$project_dir/tools" \
  "$project_dir/tools/test-darling-wayland.c" -o "$out_dir/test-darling-wayland" \
  $(pkg-config --cflags --libs egl x11) -ldl

if [ "$host_only" -eq 1 ]; then
  echo "built $out_dir/libdarling-wayland.next.so and $out_dir/test-darling-wayland"
  exit 0
fi

# The wrapper needs the Darwin SDK, which only the configured copy in build/runtime/tools knows.
clang -target x86_64-apple-macosx11.0 -fuse-ld=lld -dynamiclib -nostdlib -O2 \
  -isysroot "@DARLING_SDK@" \
  -I"$source_dir/src/startup/mldr/elfcalls" -I"$project_dir/tools" \
  "-DDARLING_WAYLAND_HOST_LIBRARY=\"$out_dir/libdarling-wayland.so\"" \
  -Wl,-undefined,dynamic_lookup "-Wl,-install_name,@GUEST_HOME@/libDarlingWayland.dylib" \
  "$project_dir/tools/darling-wayland-wrapper.c" -o "$out_dir/libDarlingWayland.next.dylib"
echo "built $out_dir/libdarling-wayland.next.so and $out_dir/libDarlingWayland.next.dylib"
