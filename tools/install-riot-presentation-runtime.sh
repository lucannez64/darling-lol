#!/bin/sh
# Build and install per-user framework overrides; restart before using them.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dir="$project_dir/build/darling-src"
build_dir="$source_dir/build"
. "$project_dir/tools/runtime-paths.sh"
mkdir -p "$project_dir/build"
make -C "$build_dir/src/external/xnu/darling/src/libsystem_kernel" system_kernel -j8
make -C "$build_dir/src/external/libplatform" platform -j8
make -C "$build_dir/src/external/corefoundation" CoreFoundation_x86_64 -j8
make -C "$build_dir/src/external/cfnetwork/src" CFNetwork -j8
make -C "$build_dir/src/external/configd/SystemConfiguration.fproj" SystemConfiguration -j8
make -C "$build_dir/src/external/cocotron/QuartzCore" QuartzCore -j8
make -C "$build_dir/src/external/cocotron/AppKit" AppKit X11_backend -j8
python3 "$project_dir/tools/build-display-timing-runtime.py"
cc -shared -fPIC -O3 -march=native -mtune=generic -Wall -Wextra \
  "-Wl,-soname,$project_dir/build/libdarling-xshm.so" \
  "$project_dir/tools/m58f299-xshm.c" -o "$project_dir/build/libdarling-xshm.next.so" \
  -pthread $(pkg-config --cflags --libs x11 x11-xcb xcb-shm xext)
clang -target x86_64-apple-macosx11.0 -fuse-ld=lld -dynamiclib -nostdlib -O3 -march=native -mtune=generic \
  -isysroot "@DARLING_SDK@" \
  -I"$source_dir/src/startup/mldr/elfcalls" \
  "-DDARLING_XSHM_HOST_LIBRARY=\"$project_dir/build/libdarling-xshm.so\"" \
  -Wl,-undefined,dynamic_lookup "-Wl,-install_name,@GUEST_HOME@/libDarlingXShm.dylib" \
  "$project_dir/tools/m58f299-native-wrapper.c" -o "$project_dir/build/libDarlingXShm.next.dylib"
# Experimental native Wayland transport for the game window (used only with RCLOL_WAYLAND=1).
# Optional: the build skips itself when the Wayland development files are missing, and a
# failed build leaves the rest of the runtime install alone.
rm -f "$project_dir/build/libdarling-wayland.next.so" "$project_dir/build/libDarlingWayland.next.dylib"
sh "$project_dir/tools/build-darling-wayland.sh" ||
  echo 'warning: the experimental Wayland transport did not build; RCLOL_WAYLAND=1 will stay on X11' >&2
# Stop before changing the overlay's upper directory, even with atomic writes.
python3 "$project_dir/tools/stop-darling-runtime.py"
mkdir -p "$guest_home"
mv -f "$project_dir/build/libdarling-xshm.next.so" "$project_dir/build/libdarling-xshm.so"
install -m755 "$project_dir/build/libDarlingXShm.next.dylib" "$guest_home/libDarlingXShm.next.dylib"
mv -f "$guest_home/libDarlingXShm.next.dylib" "$guest_home/libDarlingXShm.dylib"
if [ -f "$project_dir/build/libdarling-wayland.next.so" ] && [ -f "$project_dir/build/libDarlingWayland.next.dylib" ]; then
  mv -f "$project_dir/build/libdarling-wayland.next.so" "$project_dir/build/libdarling-wayland.so"
  install -m755 "$project_dir/build/libDarlingWayland.next.dylib" "$guest_home/libDarlingWayland.next.dylib"
  mv -f "$guest_home/libDarlingWayland.next.dylib" "$guest_home/libDarlingWayland.dylib"
  rm -f "$project_dir/build/libDarlingWayland.next.dylib"
fi
mkdir -p "$guest_home/RuntimeFrameworks"
for framework in AppKit QuartzCore CoreGraphics OpenGL CoreVideo CoreFoundation SystemConfiguration CFNetwork; do
  if [ ! -d "$guest_home/RuntimeFrameworks/$framework.framework" ]; then
    cp -a "@DARLING_SDK@/System/Library/Frameworks/$framework.framework" "$guest_home/RuntimeFrameworks/"
  fi
done
atomic_install() {
  install -m755 "$1" "$2.next"
  mv -f "$2.next" "$2"
}
atomic_install "$build_dir/src/external/cocotron/AppKit/AppKit" "$guest_home/RuntimeFrameworks/AppKit.framework/Versions/C/AppKit"
atomic_install "$project_dir/build/display-timing-runtime/X11" "$guest_home/RuntimeFrameworks/AppKit.framework/Versions/C/Resources/Backends/X11.backend/Contents/MacOS/X11"
for framework in CoreGraphics OpenGL CoreVideo QuartzCore; do
  atomic_install "$project_dir/build/display-timing-runtime/$framework" "$guest_home/RuntimeFrameworks/$framework.framework/Versions/A/$framework"
done

atomic_install "$build_dir/src/external/corefoundation/CoreFoundation" "$guest_home/RuntimeFrameworks/CoreFoundation.framework/Versions/A/CoreFoundation"

atomic_install "$build_dir/src/external/configd/SystemConfiguration.fproj/SystemConfiguration" "$guest_home/RuntimeFrameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration"

atomic_install "$build_dir/src/external/cfnetwork/src/CFNetwork" "$guest_home/RuntimeFrameworks/CFNetwork.framework/Versions/A/CFNetwork"

mkdir -p "$guest_home/RuntimeLibraries"
atomic_install "$build_dir/src/external/libplatform/libsystem_platform.dylib" "$guest_home/RuntimeLibraries/libsystem_platform.dylib"
atomic_install "$build_dir/src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib" "$guest_home/RuntimeLibraries/libsystem_kernel.dylib"

# Bootstrap services can start before the app's DYLD_LIBRARY_PATH is applied.
# The prefix overlay is user-owned; install the same corrected kernel there.
mkdir -p "$prefix_dir/usr/lib/system"
atomic_install "$build_dir/src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib" "$prefix_dir/usr/lib/system/libsystem_kernel.dylib"

echo "Presentation runtime installed; launch scripts/run.sh."
