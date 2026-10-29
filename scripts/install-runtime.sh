#!/bin/sh
# Builds and installs the paired per-user runtime; does not copy game files.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
if [ -n "${DARLING_WORKSPACE:-}" ]; then
  python3 scripts/nix-idle.py
  python3 scripts/nix-state.py check-base || { echo 'Base runtime readiness missing; run darling-lol --update.' >&2; exit 1; }
  python3 scripts/nix-state.py invalidate
  # Prefix initialization is a setup-only exception to the runner launch gate.
  export DARLING_SETUP=1
fi
python3 scripts/configure.py
. build/runtime/tools/runtime-paths.sh
if [ -d build/venv/bin ]; then PATH="$repo/build/venv/bin:$PATH"; export PATH; fi
# Check versions before any runtime helper touches the prefix or the running session.
[ "${RCLOL_SKIP_PREFLIGHT:-0}" = 1 ] || python3 scripts/preflight.py
# A server killed by the base install leaves a stale .init.pid that makes darling shell fail.
python3 build/runtime/tools/ensure-darling-runtime.py
# Initialize the normal user-owned prefix before installing overrides.
# Darling treats a missing prefix directory as new; only then is an identity generated below.
new_prefix=
[ -e "$prefix_dir" ] || new_prefix=1
darling shell /usr/bin/true
# The prefix has no /private/tmp of its own, so the guest falls through to the root-owned
# installed layer and every write there (the Riot installer's temp files) fails with EACCES.
prefix=$prefix_dir
mkdir -p "$prefix/private/tmp"
chmod 1777 "$prefix/private/tmp"
# The single hardware-presentation profile lives in the prefix so it survives
# updates and can be edited without touching the checkout. Never replace an
# existing profile: an update must not turn a user's HOST/COMPAT choice back on.
if [ ! -e "$prefix/private/etc/darling-system.prop" ]; then
  mkdir -p "$prefix/private/etc"
  install -m 0644 build/runtime/tools/system.prop "$prefix/private/etc/darling-system.prop"
fi
# Build the system root store now that the prefix exists; Darling is stopped next anyway.
python3 build/runtime/tools/ensure-darling-root-certificates.py || \
  echo 'Darling system root store not installed; the launcher retries it' >&2
python3 build/runtime/tools/stop-darling-runtime.py
# Give a new prefix its own synthetic serial, MLB, UUID and ROM instead of the host-derived defaults.
# Existing prefixes keep their identity; update.sh only replaces the old shared serial and UUID.
if [ -n "$new_prefix" ]; then
  python3 tools/configure-darling-platform.py --prefix "$prefix_dir" --provision --write || \
    echo 'Platform identity not provisioned; run tools/configure-darling-platform.py --provision --write' >&2
fi
sh build/runtime/tools/install-riot-presentation-runtime.sh
sh build/runtime/tools/install-riot-gpu-runtime.sh
# darling-lol perf: build and install optimized libc/libc++/libplatform.
if [ -f build/runtime/tools/build-riot-libs-runtime.sh ]; then
  sh build/runtime/tools/build-riot-libs-runtime.sh
  python3 build/runtime/tools/install-riot-libs-runtime.py
fi
python3 build/runtime/tools/install-darling-system-profiler.py
# Every guest process loads these overlays; refuse a set the compiler broke (check-darwin-tlv-calls.py).
python3 build/runtime/tools/validate-riot-runtime.py
# Mark this canonical prefix and configured guest ready only after validation.
if [ -n "${DARLING_WORKSPACE:-}" ]; then
  # Overlay installation can also refresh darlingserver; record the final base.
  python3 scripts/nix-state.py stamp-base
  python3 scripts/nix-state.py stamp
fi
