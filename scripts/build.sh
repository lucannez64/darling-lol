#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
# Refuse before the long fetch and build when the host cannot build or run the result.
# Runs before configure.py so an old interpreter gets this message, not a traceback.
[ "${RCLOL_SKIP_PREFLIGHT:-0}" = 1 ] || python3 scripts/preflight.py
python3 scripts/configure.py
if [ -d build/venv/bin ]; then PATH="$repo/build/venv/bin:$PATH"; export PATH; fi
# Cap the submodule fetch jobs: more saturates the disk/network and GitHub
# refuses many simultaneous connections. Positive values pass through; 0, an
# empty value or other invalid input falls back to the cap.
prepare_jobs=${RCLOL_PREPARE_JOBS:-$(( $(nproc) > 8 ? 8 : $(nproc) ))}
case $prepare_jobs in ''|0|*[!0-9]*) prepare_jobs=8;; esac
case $prepare_jobs in *[!0]*) ;; *) prepare_jobs=8;; esac
python3 scripts/prepare.py --jobs "$prepare_jobs"
# Empty build type, as upstream Darling uses: objc4 always defines OBJC_IS_DEBUG_BUILD=1,
# so Release's -DNDEBUG trips its "mismatch in debug-ness macros" #error.
# x86-64 only: the patched OpenDirectory declares ivars in @implementation, which the
# 32-bit (fragile ABI) slice cannot compile, and the macOS client is 64-bit.
# darling-lol perf: inject -O3 and -march=native even with empty build type,
# so the base Darling runtime is not compiled at -O0.  The empty build type
# is required by objc4's debug-ness macro check.
# find_library() keeps whatever a previous configure cached without checking that the
# files still exist, so a build directory reused across environments (host vs. toolbox
# container, or a system upgrade that moved the libraries) poisons the build: configure
# "finds" a library that is not there anymore and make only fails at link time (e.g.
# mldr against /usr/lib/libxml2.so, which lives at /usr/lib64 on Fedora). Nothing is
# removed automatically because the directory may hold useful partial work; if the
# build fails, deleting build/darling-src/build and rerunning reconfigures fresh.
# The value names come from a bracket that does not exclude anything CMake puts in
# an entry name: dashes, dots and pluses appear (e.g. Some-Dashed.var:FILEPATH=...).
cache=build/darling-src/build/CMakeCache.txt
if [ -f "$cache" ]; then
  missing=$(sed -n 's/^[^:#/][^:]*:FILEPATH=//p' "$cache" |
    while IFS= read -r path; do
      case $path in ''|*-NOTFOUND) continue ;; esac
      case $path in /*) [ -e "$path" ] || printf '%s\n' "$path" ;; esac
    done | sort -u)
  if [ -n "$missing" ]; then
    printf 'warning: %s comes from a previous run and caches paths that do not exist in this environment:\n%s\n' "$cache" "$missing" >&2
    printf 'warning: expect "no rule to make target" style link failures; delete build/darling-src/build and run this script again to reconfigure from scratch.\n' >&2
  fi
fi
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
