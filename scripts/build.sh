#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
# Refuse before the long fetch and build when the host cannot build or run the result.
# Runs before configure.py so an old interpreter gets this message, not a traceback.
[ "${RCLOL_SKIP_PREFLIGHT:-0}" = 1 ] || python3 scripts/preflight.py
python3 scripts/configure.py
if [ -d build/venv/bin ]; then PATH="$repo/build/venv/bin:$PATH"; export PATH; fi
python3 scripts/prepare.py
# Empty build type, as upstream Darling uses: objc4 always defines OBJC_IS_DEBUG_BUILD=1,
# so Release's -DNDEBUG trips its "mismatch in debug-ness macros" #error.
# x86-64 only: the patched OpenDirectory declares ivars in @implementation, which the
# 32-bit (fragile ABI) slice cannot compile, and the macOS client is 64-bit.
# darling-lol perf: inject -O3 and -march=native even with empty build type,
# so the base Darling runtime is not compiled at -O0.  The empty build type
# is required by objc4's debug-ness macro check.
# -mtune=generic, not native: clang 23 tuning for AMD Zen (znver*) passes the thread-local
# variable descriptor in the wrong register on Darwin targets, so every __thread access
# returns NULL (launchd crashed in libkqueue's epoll_event_dump).
cmake -S build/darling-src -B build/darling-src/build -G 'Unix Makefiles' \
  -DCMAKE_BUILD_TYPE= -DTARGET_i386=OFF -DCMAKE_INSTALL_PREFIX=/usr/local -DENABLE_METAL=ON \
  -DCMAKE_C_FLAGS="-O3 -march=native -mtune=generic -pipe" \
  -DCMAKE_CXX_FLAGS="-O3 -march=native -mtune=generic -pipe"
cmake --build build/darling-src/build --parallel "${JOBS:-$(nproc)}"
if [ -n "${DARLING_WORKSPACE:-}" ]; then
  echo 'Build complete. Install the base runtime with: cmake --install build/darling-src/build'
else
  echo 'Build complete. Install the base runtime with: sudo cmake --install build/darling-src/build'
fi
