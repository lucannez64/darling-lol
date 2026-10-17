#!/bin/sh
# darling-lol M58F101: launch the real Riot chain with the argv log-dir shim.
#
# The Riot Foundation starts the UX product with a home-relative
# `--log-dir=Library/Logs/Riot Games/Riot Client`; the app hands that to
# Electron's app.setAppLogsPath(), which THROWS "Path must be absolute", so the
# UX stalls before app.whenReady()/createWindow() and never maps its window.
#
# The shim (tools/m58f101-logdir-shim.c) rewrites `--log-dir=<relative>` to
# `--log-dir=$HOME/<relative>` in argv at load time.  The product is NOT
# hardened, so DYLD_INSERT_LIBRARIES set on the launcher propagates through the
# Foundation to the product.
#
# One-time setup:
#   clang -target x86_64-apple-macosx11.0 -fuse-ld=lld -dynamiclib -nostdlib \
#     -nodefaultlibs -undefined dynamic_lookup -O2 -o /tmp/zzlogdir.dylib \
#     tools/m58f101-logdir-shim.c -Wl,-install_name,/tmp/zzlogdir.dylib
#   cp /tmp/zzlogdir.dylib "$HOME/.darling@GUEST_HOME@/zzlogdir.dylib"
#   # -> guest path @GUEST_HOME@/zzlogdir.dylib (outside the app bundle, so the
#   #    launcher's install-integrity check is unaffected)
#
# Then run this script (guest DISPLAY defaults to :1).
# Darling's native window backend supplies Xlib Display pointers to EGL.
# Select X11 explicitly so Mesa does not choose Wayland from the host session.
set -eu
# Native guest crashes created large system core files and exhausted this disk.
# Address-only diagnostics suffice; bound this runner and its new namespace.
if [ "${RCLOL_CORE_DUMPS:-0}" != 1 ]; then
 ulimit -c 0
fi
runner_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$runner_dir/ensure-darling-runtime.py"
python3 "$runner_dir/ensure-darling-service-accounts.py"
python3 "$runner_dir/ensure-league-runtime-layout.py"
# Chromium's native macOS verifier stalls in Darling's Security/trustd path.
# Use its built-in verifier; certificate validation remains enabled.
# Keep the hardware-acceleration switch. The machine-id slot is Sentry
# metadata; reuse it for the verifier switch. Each replacement is one argv entry.
# Additional diagnostics can use RCLOL_APP_ARG2 with an explicit
# RCLOL_APP_ARG2_SLOT identifying an existing expendable argument.
RCLOL_APP_ARGS=${RCLOL_APP_ARGS-'--enable-features=ChromeRootStoreUsed,CertVerifierBuiltin:impl/4'}
RCLOL_APP_ARGS_SLOT=${RCLOL_APP_ARGS_SLOT-'--machine-id='}
export RCLOL_APP_ARGS RCLOL_APP_ARGS_SLOT
RCLOL_APP_ARG2=${RCLOL_APP_ARG2-'--disable-features=RemoteCoreAnimationAPI,AudioServiceOutOfProcess,UseVideoFrameSinkBundle'}
RCLOL_APP_ARG2_SLOT=${RCLOL_APP_ARG2_SLOT-'--crashpad-environment='}
export RCLOL_APP_ARG2 RCLOL_APP_ARG2_SLOT
# Client inspectors and automatic UI hooks are excluded from normal launches.
unset RCLOL_APP_ARG3 RCLOL_APP_ARG3_SLOT RCLOL_UX_ARG2 RCLOL_UX_ARG2_SLOT
# Keep the existing feature compatibility switch without forcing unlimited UI frames.
if [ "${RCLOL_LEAGUE_UX_TUNING:-1}" != 0 ]; then
 RCLOL_UX_ARG1='--disable-features=HardwareMediaKeyHandling,RemoteCoreAnimationAPI,UseVideoFrameSinkBundle'
 RCLOL_UX_ARG1_SLOT='--disable-features='
 export RCLOL_UX_ARG1 RCLOL_UX_ARG1_SLOT
fi
# The paired runtime presents completed GPU textures without CPU readback.
# Explicit 0 or an empty value selects the CPU fallback for diagnostics.
for option in RCLOL_GPU_DIRECT_PRESENT RCLOL_GPU_DIRECT_ASYNC RCLOL_GPU_DIRECT_VSYNC DARLING_CA_PRESENT_AT_COMMIT DARLING_CA_SKIP_LOCAL_IOSURFACE_RENDER RCLOL_FORWARD_MOUSE_MOVED; do
 eval 'value=${'"$option"'-1}'
 case "$value" in 0|'') unset "$option";; *) export "$option=$value";; esac
done
# Use Chromium's normal decoder, frame and background scheduling policies.
# Launching the client does not start inspector helpers or inject UI hooks.
SHIM_GUEST="@GUEST_HOME@/zzlogdir.dylib"
RIOT_INSERT_LIBRARIES="$SHIM_GUEST:@GUEST_HOME@/libRiotEmptyAutofill.dylib"
# Use the verified bridge when the paired host/guest artifacts are installed.
# Its UUID and interface guards retain Chromium's decoder on other builds.
native_vpx_default=0
if [ -f "$HOME/.darling@GUEST_HOME@/libRiotNativeVpx.dylib" ] && [ -f "$runner_dir/../build/libriot-native-vpx.so" ]; then
 native_vpx_default=1
fi
if [ "${RCLOL_NATIVE_VPX:-$native_vpx_default}" = 1 ]; then
 RIOT_INSERT_LIBRARIES="$RIOT_INSERT_LIBRARIES:@GUEST_HOME@/libRiotNativeVpx.dylib"
fi
if [ -n "${RIOT_LAUNCHER_HOST:-}" ]; then
 A=$RIOT_LAUNCHER_HOST
else
 A="$HOME/.darling/Applications/Riot Client.app/Contents/MacOS/RiotClientServices"
 # Riot's installer places the client in /Users/Shared. The conventional
 # /Applications symlink may be an absolute macOS path, which Linux cannot
 # resolve when Darling's launcher checks the host-side executable path.
 if [ ! -f "$A" ] && [ -f "$HOME/.darling/Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices" ]; then
  A="$HOME/.darling/Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices"
 fi
fi
# External XIM (for example Fcitx) consumes keys before Darling forwards them.
# Use Xlib's local input method; allow explicit testing of another modifier.
# Linux C.UTF-8 is not a supported codecvt locale in the macOS client.
# Keep this scoped to the guest; product region/locale arguments remain explicit.
# The first-time installer (RIOT_LAUNCHER_HOST) exits immediately when given --app-root for a
# Riot Client that is not installed yet; only pass it for the installed client.
# Its native intro screen renders image layers with a blue cover and does not take clicks
# under Darling, so start the install directly (the Riot Client UI that follows works).
if [ -n "${RIOT_LAUNCHER_HOST:-}" ]; then
 set -- --skip-to-install --launch-product=league_of_legends --launch-patchline=live
else
 set -- --app-root="/Applications/Riot Client.app"
 # The League login theme (video background, Install/Play button) is only shown when the client
 # is started for a product. RCLOL_LAUNCH_PRODUCT= (empty) starts the bare client instead.
 if [ -n "${RCLOL_LAUNCH_PRODUCT-league_of_legends}" ]; then
  set -- "$@" --launch-product="${RCLOL_LAUNCH_PRODUCT-league_of_legends}" \
    --launch-patchline="${RCLOL_LAUNCH_PATCHLINE-live}"
 fi
fi
exec env -u ELECTRON_RUN_AS_NODE EGL_PLATFORM=x11 \
  LANG="${RCLOL_GUEST_LOCALE:-en_US.UTF-8}" LC_ALL="${RCLOL_GUEST_LOCALE:-en_US.UTF-8}" \
  mesa_glthread="${RCLOL_MESA_GLTHREAD-${mesa_glthread-false}}" \
  XMODIFIERS="${RCLOL_XMODIFIERS:-@im=none}" DISPLAY="${DISPLAY:-:1}" \
  DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:-@GUEST_HOME@/RuntimeLibraries}" \
  DYLD_FRAMEWORK_PATH="${DYLD_FRAMEWORK_PATH:-@GUEST_HOME@/RuntimeFrameworks}" \
  DARLING_XSHM_LIBRARY="${DARLING_XSHM_LIBRARY:-@GUEST_HOME@/libDarlingXShm.dylib}" \
  DYLD_INSERT_LIBRARIES="$RIOT_INSERT_LIBRARIES" \
  darling "$A" --region="${RCLOL_REGION:-EUW}" --locale="${RCLOL_LOCALE:-en_GB}" \
    "$@" \
    --log-dir="@GUEST_HOME@/Library/Logs/Riot Games/Riot Client"
