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
if [ "${RCLOL_SKIP_CONFIGURE:-0}" != 1 ] || [ ! -f build/runtime/tools/runtime-paths.sh ]; then
  python3 scripts/configure.py
fi
. build/runtime/tools/runtime-paths.sh
if [ -d build/venv/bin ]; then PATH="$repo/build/venv/bin:$PATH"; export PATH; fi
# Check versions before any runtime helper touches the prefix or the running session.
[ "${RCLOL_SKIP_PREFLIGHT:-0}" = 1 ] || python3 scripts/preflight.py
runtime_stopped=${RCLOL_RUNTIME_STOPPED:-0}
# A server killed by the base install leaves a stale .init.pid that makes darling shell fail.
if [ "$runtime_stopped" != 1 ]; then
  python3 build/runtime/tools/ensure-darling-runtime.py
fi
# Initialize the normal user-owned prefix before installing overrides.
# Darling treats a missing prefix directory as new; only then is an identity generated below.
new_prefix=
[ -e "$prefix_dir" ] || new_prefix=1
# A boot is only needed to create the prefix: an initialized prefix leaves its
# system.prop behind as proof, so skip the full guest boot on re-installs.
if [ -n "$new_prefix" ] || [ ! -e "$prefix_dir/private/etc/darling-system.prop" ]; then
  darling shell /usr/bin/true
  runtime_stopped=0
fi
# The prefix has no /private/tmp of its own, so the guest falls through to the root-owned
# installed layer and every write there (the Riot installer's temp files) fails with EACCES.
prefix=$prefix_dir
mkdir -p "$prefix/private/tmp"
chmod 1777 "$prefix/private/tmp"
# The single hardware-presentation profile lives in the prefix so it survives
# updates and can be edited without touching the checkout. Never replace an
# existing profile: an update must not undo a user's HOST/COMPAT choice.
if [ ! -e "$prefix/private/etc/darling-system.prop" ]; then
  mkdir -p "$prefix/private/etc"
  install -m 0644 build/runtime/tools/system.prop "$prefix/private/etc/darling-system.prop"
fi
# One-time move of profiles installed while HOST was the default (issue #31).
python3 build/runtime/tools/migrate-system-profile.py "$prefix/private/etc/darling-system.prop" || \
  echo "Hardware profile not migrated; set profile=compat in $prefix/private/etc/darling-system.prop" >&2
# Build the system root store now that the prefix exists; if certificates are
# not already current, the helper may start Darling and must be followed by a stop.
if [ "$runtime_stopped" = 1 ]; then
  if ! python3 build/runtime/tools/ensure-darling-root-certificates.py --prefix "$prefix_dir" --check; then
    runtime_stopped=0
    python3 build/runtime/tools/ensure-darling-root-certificates.py --prefix "$prefix_dir" || \
      echo 'Darling system root store not installed; the launcher retries it' >&2
  fi
else
  python3 build/runtime/tools/ensure-darling-root-certificates.py --prefix "$prefix_dir" || \
    echo 'Darling system root store not installed; the launcher retries it' >&2
fi
if [ "$runtime_stopped" != 1 ]; then
  python3 build/runtime/tools/stop-darling-runtime.py
  runtime_stopped=1
fi
# Give a new prefix its own synthetic serial, MLB, UUID and ROM instead of the host-derived defaults.
# Existing prefixes keep their identity; update.sh only replaces the old shared serial and UUID,
# while every existing prefix still gets its SystemVersion.plist aligned to the emulated macOS version.
if [ -n "$new_prefix" ]; then
  python3 tools/configure-darling-platform.py --prefix "$prefix_dir" --provision --write || \
    echo 'Platform identity not provisioned; run tools/configure-darling-platform.py --provision --write' >&2
else
  python3 tools/configure-darling-platform.py --prefix "$prefix_dir" --normalize-system-version --write || \
    echo 'SystemVersion.plist not normalized; run tools/configure-darling-platform.py --normalize-system-version --write' >&2
fi
# Rebuild and install every per-user overlay. This is the expensive half of an
# update: the CMake targets and overlay builders relink unconditionally, so a
# pull that changes nothing they consume would still spend about a minute here.
install_overlay_batch() {
  RCLOL_RUNTIME_STOPPED=1 sh build/runtime/tools/install-riot-presentation-runtime.sh
  RCLOL_RUNTIME_STOPPED=1 RCLOL_PRESENTATION_BUILT_XSHM=1 sh build/runtime/tools/install-riot-gpu-runtime.sh
  # darling-lol perf: build and install optimized libc/libc++/libplatform.
  if [ -f build/runtime/tools/build-riot-libs-runtime.sh ]; then
    sh build/runtime/tools/build-riot-libs-runtime.sh
    python3 build/runtime/tools/install-riot-libs-runtime.py
  fi
  python3 build/runtime/tools/install-darling-system-profiler.py
  # Riot Client 142 (Electron 43) hard-links QuickLookUI, MediaPlayer, CoreML, CoreHaptics and
  # Accessibility; without these stubs in RuntimeFrameworks dyld aborts before the first window.
  python3 tools/build-riot-electron43-frameworks.py --install
}
# The stamp covers the patched checkout, the builder sources, the configured
# flags, the artifacts above and the installed overlays (recorded after signing).
overlay_state=build/runtime/overlay-stamp.json
overlay_rebuilt=
signing_ok=0
if [ "${RCLOL_FORCE_OVERLAY_REBUILD:-0}" = 1 ] ||
  ! python3 build/runtime/tools/runtime-overlay-stamp.py --root "$repo" check --state "$overlay_state"; then
  install_overlay_batch
  overlay_rebuilt=1
else
  echo 'Runtime overlays are up to date; skipping the overlay rebuild.'
fi
# Every guest process loads these overlays; refuse a set the compiler broke (check-darwin-tlv-calls.py).
# When the overlay stamp already matches (including the installed overlays, canonical copies,
# and validation/sync scripts), skip the serial disassembly and byte-compare pass unless forced.
if [ -n "$overlay_rebuilt" ] || [ "${RCLOL_FORCE_VALIDATE:-0}" = 1 ]; then
  python3 build/runtime/tools/validate-riot-runtime.py
  # Mirror per-user overlays to canonical prefix paths (/System/Library/Frameworks,
  # /usr/lib) so the launcher can run without DYLD_LIBRARY_PATH/DYLD_FRAMEWORK_PATH.
  python3 "$repo/tools/sync-canonical-runtime-overlays.py" --prefix "$prefix_dir" --guest-home "$guest_home"
else
  echo 'Runtime overlays already validated and synced; skipping validation and canonical mirror.'
fi
# Sign the installed runtime and populate the local trust store. The base SDK
# is signed only when this process can actually write it; the per-user
# RuntimeLibraries/RuntimeFrameworks overlays are always signed.
if [ -x "$repo/tools/sign-darling-runtime.sh" ]; then
  sign_args="--prefix --quiet"
  stamp_sdk=
  if [ -w /usr/local/libexec/darling ]; then
    sign_args="--sdk $sign_args"
    stamp_sdk=--sdk
  fi
  sign_stamp=build/runtime-sign-stamp.json
  # Signing rewrites every Mach-O and a writable SDK pass also rewrites the
  # trust store, so an unchanged stamp means the trees still carry the
  # signatures the last successful pass left. The stamp covers exactly the
  # roots the pass selects; a rebuilt overlay, a re-synced canonical copy, a
  # new submodule or a removed trust store all change it. RCLOL_FORCE_SIGN=1
  # bypasses the stamp and signs again.
  if [ "${RCLOL_FORCE_SIGN:-0}" != 1 ] &&
    python3 build/runtime/tools/runtime-sign-stamp.py --root "$repo" \
      --prefix "$prefix_dir" $stamp_sdk --state "$sign_stamp" check; then
    echo 'Runtime overlays are signed; skipping the signing pass.'
    signing_ok=1
  else
    if [ ! -x "$repo/tools/dc-codesign/dc-codesign" ]; then
      make -s -C "$repo/tools/dc-codesign" dc-codesign || \
        echo 'dc-codesign did not build (it needs the OpenSSL headers)' >&2
    fi
    # Unsigned overlays are loaded into every Riot process (issue #31): fetch a
    # pinned rcodesign when none is installed. An explicit RCODESIGN wins.
    if [ -z "${RCODESIGN:-}" ] && rcodesign_path=$(sh "$repo/tools/install-rcodesign.sh"); then
      export RCODESIGN="$rcodesign_path"
    fi
    if ! sh "$repo/tools/sign-darling-runtime.sh" $sign_args; then
      cat >&2 <<'EOF'

==================================================================
 WARNING: the darling-lol runtime is NOT signed.
 Riot's processes load these libraries unsigned, which a real Mac
 never does. Fix the error above (usually: install rcodesign with
 tools/install-rcodesign.sh or `cargo install apple-codesign`),
 then run the update again before you play.
==================================================================

EOF
    else
      signing_ok=1
      python3 build/runtime/tools/runtime-sign-stamp.py --root "$repo" \
        --prefix "$prefix_dir" $stamp_sdk --state "$sign_stamp" record || \
        echo 'Signing stamp not recorded; the next update signs the overlays again' >&2
    fi
  fi
fi
# Only a completed install and a successful signing pass may vouch for the next
# update's fast path. The stamp is taken after signing, which rewrites the
# overlay Mach-Os, and a failed pass has to leave it unrecorded so the next
# update installs and signs the overlays again.
if [ -n "$overlay_rebuilt" ] && [ "$signing_ok" = 1 ]; then
  # The stubs were copied before signing; record what is on disk now so the next
  # batch keeps that signed copy instead of replacing and re-signing it.
  python3 tools/build-riot-electron43-frameworks.py --record-installed || true
  python3 build/runtime/tools/runtime-overlay-stamp.py --root "$repo" record --state "$overlay_state" || \
    echo 'Runtime overlay stamp not recorded; the next update rebuilds the overlays' >&2
fi
# Mark this canonical prefix and configured guest ready only after validation.
if [ -n "${DARLING_WORKSPACE:-}" ]; then
  # Overlay installation can also refresh darlingserver; record the final base.
  python3 scripts/nix-state.py stamp-base
  python3 scripts/nix-state.py stamp
fi
