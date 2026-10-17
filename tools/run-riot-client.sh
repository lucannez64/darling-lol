#!/bin/sh
# Launch Riot Client using the Darling runtime, without library injection.
# Darling supplies Xlib Display pointers to EGL, so select X11 explicitly.
set -eu
# Native guest crashes created large system core files and exhausted this disk.
# Address-only diagnostics suffice; bound this runner and its new namespace.
if [ "${RCLOL_CORE_DUMPS:-0}" != 1 ]; then
 ulimit -c 0
fi
runner_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${RCLOL_RECOVER_HIDDEN_CLIENT:-0}" = 1 ]; then
 python3 "$runner_dir/recover-hidden-riot-client.py"
fi
# The bundled downloader rejects IPv6 CONNECT_TO addresses. Only Riot CDN
# AAAA lookups are suppressed; set this to 0 to retain the upstream resolver.
export RCLOL_CDN_IPV4_ONLY="${RCLOL_CDN_IPV4_ONLY:-1}"
python3 "$runner_dir/ensure-darling-runtime.py"
python3 "$runner_dir/ensure-darling-service-accounts.py"
python3 "$runner_dir/ensure-league-runtime-layout.py"
# Discard argument-rewriting settings from the former injected shim.
unset RCLOL_APP_ARGS RCLOL_APP_ARGS_SLOT RCLOL_APP_ARG2 RCLOL_APP_ARG2_SLOT
unset RCLOL_APP_ARG3 RCLOL_APP_ARG3_SLOT RCLOL_UX_ARG1 RCLOL_UX_ARG1_SLOT
unset RCLOL_UX_ARG2 RCLOL_UX_ARG2_SLOT
# The paired runtime presents completed GPU textures without CPU readback.
# Explicit 0 or an empty value selects the CPU fallback for diagnostics.
for option in RCLOL_GPU_DIRECT_PRESENT RCLOL_GPU_DIRECT_ASYNC RCLOL_GPU_DIRECT_VSYNC DARLING_CA_PRESENT_AT_COMMIT DARLING_CA_SKIP_LOCAL_IOSURFACE_RENDER RCLOL_FORWARD_MOUSE_MOVED RCLOL_MOUSE_CONFINEMENT; do
 eval 'value=${'"$option"'-1}'
 case "$value" in 0|'') unset "$option";; *) export "$option=$value";; esac
done
prefix=${DPREFIX:-$HOME/.darling}
# The first-time installer puts the client in /Users/Shared/Riot Games and then
# hands off through LaunchServices, which does not start it under Darling. Link
# the installed client where the normal launch expects it.
client="$prefix/Applications/Riot Client.app"
shared="$prefix/Users/Shared/Riot Games/Riot Client.app"
if [ -z "${RIOT_LAUNCHER_HOST:-}" ] && [ ! -e "$client" ]; then
 if [ ! -d "$shared" ]; then
  echo "Riot Client is not installed in $prefix; run scripts/installer.sh first" >&2
  exit 1
 fi
 if [ -L "$client" ]; then
  # An absolute link (/Users/Shared/...) resolves only inside the guest; the
  # guest still uses it for --app-root, but start the client by its real path.
  client=$shared
 else
  ln -s "../Users/Shared/Riot Games/Riot Client.app" "$client"
 fi
fi
A=${RIOT_LAUNCHER_HOST:-"$client/Contents/MacOS/RiotClientServices"}
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
# --- darling-lol perf: scheduling & driver latency hints ---
# Refresh rate for display link, async presenter, and frame pacing: the primary monitor's
# current mode (the first monitor without a primary); unset falls back to 60 Hz.
if [ -z "${RCLOL_REFRESH_HZ-}" ] && command -v xrandr >/dev/null 2>&1; then
  RCLOL_REFRESH_HZ=$(xrandr --current 2>/dev/null | awk '
    / connected/ { primary = / primary / }
    /\*/ { for (i = 2; i <= NF; i++) if ($i ~ /\*/) { rate = $i; sub(/[*+]+/, "", rate)
            if (primary) { print int(rate + 0.5); found = 1; exit } else if (first == "") first = int(rate + 0.5) } }
    END { if (!found && first != "") print first }')
fi
if [ -n "${RCLOL_REFRESH_HZ-}" ]; then export RCLOL_REFRESH_HZ; fi
# Vulkan WSI & Mesa optimizations
export MESA_NO_ERROR="${MESA_NO_ERROR:-1}"
# NVIDIA proprietary driver: limit pre-rendered frames.
export __GL_MaxFramesAllowed="${__GL_MaxFramesAllowed:-1}"
export __GL_THREADED_OPTIMIZATIONS="${__GL_THREADED_OPTIMIZATIONS:-1}"
export __GL_YIELD="${__GL_YIELD:-NOTHING}"
# Reduce timer slack for tighter scheduling in the game process tree.
if [ -w /proc/self/timerslack_ns ]; then
  echo 1 > /proc/self/timerslack_ns 2>/dev/null || true
fi
exec env -u ELECTRON_RUN_AS_NODE -u DYLD_INSERT_LIBRARIES EGL_PLATFORM=x11 \
  LANG="${RCLOL_GUEST_LOCALE:-en_US.UTF-8}" LC_ALL="${RCLOL_GUEST_LOCALE:-en_US.UTF-8}" \
  mesa_glthread="${RCLOL_MESA_GLTHREAD-${mesa_glthread-false}}" \
  XMODIFIERS="${RCLOL_XMODIFIERS:-@im=none}" DISPLAY="${DISPLAY:-:1}" \
  DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:-@GUEST_HOME@/RuntimeLibraries}" \
  DYLD_FRAMEWORK_PATH="${DYLD_FRAMEWORK_PATH:-@GUEST_HOME@/RuntimeFrameworks}" \
  DARLING_XSHM_LIBRARY="${DARLING_XSHM_LIBRARY:-@GUEST_HOME@/libDarlingXShm.dylib}" \
  darling "$A" --region="${RCLOL_REGION:-EUW}" --locale="${RCLOL_LOCALE:-en_GB}" \
    "$@" \
    --log-dir="@GUEST_HOME@/Library/Logs/Riot Games/Riot Client"
