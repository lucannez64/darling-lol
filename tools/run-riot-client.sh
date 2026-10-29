#!/bin/sh
# Launch Riot Client using the Darling runtime, without library injection.
# Darling supplies Xlib Display pointers to EGL, so select X11 explicitly.
set -eu
# Native MangoHud uses the guest PID against the host /proc view and can abort
# in GPU_fdinfo::find_fd (issue #38). Disable inherited implicit activation
# before any helper can start Darling, including first-time installation.
unset MANGOHUD
export DISABLE_MANGOHUD=1
# Native guest crashes created large system core files and exhausted this disk.
# Address-only diagnostics suffice; bound this runner and its new namespace.
if [ "${RCLOL_CORE_DUMPS:-0}" != 1 ]; then
 ulimit -c 0
fi
runner_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$runner_dir/runtime-paths.sh"

# Hardware presentation is selected in one key=value file. The installed copy
# in the prefix wins so users can change it without editing the checkout; the
# repository copy is the fallback before install-runtime.sh has run.
system_prop=${RCLOL_SYSTEM_PROP:-"$prefix_dir/private/etc/darling-system.prop"}
[ -r "$system_prop" ] || system_prop="$runner_dir/system.prop"
profile=host
host_cpu_brand=host
host_gpu_name=host
host_gpu_vram=host
host_gpu_unified=host
host_memory_bytes=host
compat_cpu_brand=
compat_gpu_name=
compat_gpu_vram=
compat_gpu_unified=
compat_memory_bytes=
if [ -r "$system_prop" ]; then
 while IFS='=' read -r key value; do
  case "$key" in
   profile) profile=$value;;
   host.cpu.brand) host_cpu_brand=$value;;
   host.gpu.name) host_gpu_name=$value;;
   host.gpu.vram) host_gpu_vram=$value;;
   host.gpu.unified) host_gpu_unified=$value;;
   host.memory.bytes) host_memory_bytes=$value;;
   compat.cpu.brand) compat_cpu_brand=$value;;
   compat.gpu.name) compat_gpu_name=$value;;
   compat.gpu.vram) compat_gpu_vram=$value;;
   compat.gpu.unified) compat_gpu_unified=$value;;
   compat.memory.bytes) compat_memory_bytes=$value;;
  esac
 done < "$system_prop"
fi
profile=${RCLOL_PLATFORM_PROFILE:-$profile}
case "$profile" in
 host|compat) ;;
 *) echo "system.prop: invalid profile '$profile'; using host" >&2; profile=host;;
esac

host_cpu_brand_default=$(awk -F: '
 /^model name[ \t]*:/ { value=$2; sub(/^[ \t]+/, "", value); print value; exit }
 /^Hardware[ \t]*:/ { value=$2; sub(/^[ \t]+/, "", value); print value; exit }
' /proc/cpuinfo 2>/dev/null || true)
host_memory_bytes_default=$(awk '/^MemTotal:/ { printf "%.0f", $2 * 1024; exit }' /proc/meminfo 2>/dev/null || true)
case "$host_cpu_brand" in host|'') host_cpu_brand=$host_cpu_brand_default;; esac
case "$host_memory_bytes" in host|'') host_memory_bytes=$host_memory_bytes_default;; esac
[ -n "$host_cpu_brand" ] || host_cpu_brand=$(uname -m 2>/dev/null || echo 'Unknown CPU')
case "$host_memory_bytes" in ''|*[!0-9]*) host_memory_bytes=;; esac

case "$profile" in
 compat)
  cpu_brand=$compat_cpu_brand
  gpu_name=$compat_gpu_name
  gpu_vram=$compat_gpu_vram
  gpu_unified=$compat_gpu_unified
  memory_bytes=$compat_memory_bytes
  ;;
 host)
  cpu_brand=$host_cpu_brand
  gpu_name=$host_gpu_name
  gpu_vram=$host_gpu_vram
  gpu_unified=$host_gpu_unified
  memory_bytes=$host_memory_bytes
  ;;
esac
export RCLOL_PLATFORM_PROFILE="$profile"
[ -z "$cpu_brand" ] || export RCLOL_CPU_BRAND="$cpu_brand"
case "$memory_bytes" in ''|*[!0-9]*) ;; *) export RCLOL_MEMORY_BYTES="$memory_bytes";; esac
export RCLOL_GPU_NAME="$gpu_name"
export RCLOL_GPU_VRAM_MB="$gpu_vram"
export RCLOL_GPU_UNIFIED="$gpu_unified"
if [ "$profile" = compat ]; then
 case "$gpu_name" in
  mac) export DARLING_METAL_MAC_GPU_NAME=1;;
  host|'') ;;
  *) export DARLING_METAL_GPU_NAME="$gpu_name";;
 esac
 case "$gpu_vram" in ''|*[!0-9]*) ;; *) export DARLING_METAL_VRAM_MB="$gpu_vram";; esac
 # The kernel patch defaults to unified memory; 0 is the compatibility value.
 export DARLING_METAL_HOST_UNIFIED_MEMORY=0
else
 # HOST leaves the Metal name to the real host card and asks it to report the
 # host's memory model. User-supplied DARLING_METAL_* overrides still win.
 export DARLING_METAL_HOST_UNIFIED_MEMORY="${DARLING_METAL_HOST_UNIFIED_MEMORY:-1}"
fi

# Opt-in partial recovery: if Darling is alive but a previous client tree is
# windowless, stop only that client/CEF tree and reuse the runtime. Never
# touches the game, darlingserver or the runtime sockets.
if [ "${RCLOL_RECOVER_STALE_CLIENT:-0}" = 1 ]; then
 python3 "$runner_dir/recover-stale-client.py"
fi
if [ "${RCLOL_RECOVER_HIDDEN_CLIENT:-0}" = 1 ]; then
 python3 "$runner_dir/recover-hidden-riot-client.py"
fi
# Opt-in cleanup of helpers left by an earlier launcher. This runs before the
# current helper is started. It removes only helpers of this prefix whose
# parent is init/systemd, so a helper of a live launcher is not touched.
if [ "${RCLOL_CLEAN_STALE_HELPERS:-0}" = 1 ]; then
 python3 "$runner_dir/clean-stale-helpers.py" --prefix "$prefix_dir" ||
  echo 'clean-stale-helpers: cleanup failed; continuing' >&2
fi
# The bundled downloader rejects IPv6 CONNECT_TO addresses. Only Riot CDN
# AAAA lookups are suppressed; set this to 0 to retain the upstream resolver.
export RCLOL_CDN_IPV4_ONLY="${RCLOL_CDN_IPV4_ONLY:-1}"
# Capture ownership before the certificate helper can start this session's runtime.
runtime_at_start=$(python3 "$runner_dir/session-supervisor.py" --prefix "$prefix_dir" --probe-runtime)
python3 "$runner_dir/ensure-darling-runtime.py"
python3 "$runner_dir/ensure-darling-service-accounts.py"
python3 "$runner_dir/ensure-league-runtime-layout.py"
# Security's system trust domain (SecTrustSettingsCopyCertificates) needs a root keychain and
# trust record built from the host CA bundle. Only a changed bundle starts Darling; a failure
# is reported but does not stop the launch.
python3 "$runner_dir/ensure-darling-root-certificates.py" ||
 echo 'Continuing without a Darling system root store; TLS clients may report untrusted roots' >&2
# Discard argument-rewriting settings from the former injected shim.
unset RCLOL_APP_ARGS RCLOL_APP_ARGS_SLOT RCLOL_APP_ARG2 RCLOL_APP_ARG2_SLOT
unset RCLOL_APP_ARG3 RCLOL_APP_ARG3_SLOT RCLOL_UX_ARG1 RCLOL_UX_ARG1_SLOT
unset RCLOL_UX_ARG2 RCLOL_UX_ARG2_SLOT
# The paired runtime presents completed GPU textures without CPU readback.
# Explicit 0 or an empty value selects the CPU fallback for diagnostics.
# Metal values come from tools/system.prop: HOST reports the real host GPU;
# COMPAT is the opt-in MacBookPro16,1 presentation.
for option in RCLOL_GPU_DIRECT_PRESENT RCLOL_GPU_DIRECT_ASYNC RCLOL_GPU_DIRECT_VSYNC DARLING_CA_PRESENT_AT_COMMIT DARLING_CA_SKIP_LOCAL_IOSURFACE_RENDER RCLOL_FORWARD_MOUSE_MOVED RCLOL_MOUSE_CONFINEMENT; do
 eval 'value=${'"$option"'-1}'
 case "$value" in 0|'') unset "$option";; *) export "$option=$value";; esac
done
prefix=$prefix_dir
# Resolve the loader before the environment filter replaces PATH with the Mac layout.
darling_bin=$(command -v darling) || {
 echo 'darling loader not found in PATH' >&2
 exit 1
}
# RIOT_GUEST_HOME is host-side; DYLD paths must be relative to the guest root.
case "$guest_home" in
 "$prefix"/*) guest_path=/${guest_home#"$prefix"/} ;;
 *) echo "RIOT_GUEST_HOME must be inside DPREFIX" >&2; exit 1 ;;
esac
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
# Opt-in automatic client-in-game handling. Start the watcher only after the
# installed-client check above: a failed installer launch must not leave a
# helper behind. The preference selector returns no action for missing or
# invalid close-during-game values.
if [ -z "${RIOT_LAUNCHER_HOST:-}" ]; then
 if [ "${RCLOL_CLIENT_IN_GAME_TRICKS:-0}" = 1 ]; then
  pref="$prefix_dir/Applications/League of Legends.app/Contents/LoL/Config/LCULocalPreferences.yaml"
  mode=$(python3 "$runner_dir/client-in-game-tricks.py" "$pref" 2>/dev/null || echo none)
  case "$mode" in
   close)
    echo 'client-in-game-tricks: closeLeagueClientDuringGame=true -> close helper' >&2
    python3 "$runner_dir/close-league-client-in-game.py" --detach &
    ;;
   hide)
    echo 'client-in-game-tricks: closeLeagueClientDuringGame=false -> hide helper' >&2
    python3 "$runner_dir/hide-league-client-in-game.py" --detach &
    ;;
   *)
    echo 'client-in-game-tricks: no action (client optimizations disabled or preferences missing/invalid)' >&2
    ;;
  esac
 elif [ "${RCLOL_CLOSE_CLIENT_IN_GAME:-0}" = 1 ]; then
  python3 "$runner_dir/close-league-client-in-game.py" --detach &
 elif [ "${RCLOL_HIDE_CLIENT_IN_GAME:-0}" = 1 ]; then
  python3 "$runner_dir/hide-league-client-in-game.py" --detach &
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
# Mesa resolves its shader disk cache below $HOME, which inside the guest is
# /Users/<user>: that path does not exist on the host filesystem, so every process logs
# "Failed to create /Users for shader cache (Permission denied)---disabling" and
# recompiles every shader on start (212 such lines in one launcher log). The host Mesa
# loaded into the guest reads MESA_SHADER_CACHE_DIR verbatim, so point it at a host
# directory; Mesa creates mesa_shader_cache/ and radv_builtin_shaders/ below it.
cache_root=${XDG_CACHE_HOME:-${HOME:-$prefix_dir}/.cache}/darling-lol
MESA_SHADER_CACHE_DIR="${MESA_SHADER_CACHE_DIR:=$cache_root}"
export MESA_SHADER_CACHE_DIR
mkdir -p "$MESA_SHADER_CACHE_DIR" 2>/dev/null || true
# NVIDIA proprietary driver: limit pre-rendered frames.
export __GL_MaxFramesAllowed="${__GL_MaxFramesAllowed:-1}"
# Threaded optimizations stay off: with them, NVIDIA 580 (the last branch for
# Pascal GPUs) with egl-x11 1.0.6 rejects the X11 EGLDisplay with EGL_BAD_DISPLAY
# on every thread that has a context current, and the game window stays black.
export __GL_THREADED_OPTIMIZATIONS="${__GL_THREADED_OPTIMIZATIONS:-0}"
export __GL_YIELD="${__GL_YIELD:-NOTHING}"
# Reduce timer slack for tighter scheduling in the game process tree.
if [ -w /proc/self/timerslack_ns ]; then
  echo 1 > /proc/self/timerslack_ns 2>/dev/null || true
fi
# On affected setups Chromium's default temp directory is unusable. Electron
# can copy app.asar entries to temporary files for async reads and report a
# failed copy as "not found". Use a writable guest directory by default.
# exec-guest.py closes host file descriptors above stderr before starting the guest. A shell
# loop cannot: dash only accepts single-digit descriptors in redirections.
# Keep the launcher as a supervisor: on Ctrl+C/terminal close it stops the
# guest session instead of orphaning launchd/mldr and the client tree. It also
# shuts the runtime down after the loader exits, unless another session already
# owned it. Set RCLOL_CLEAN_ON_EXIT=0 for the previous exec-only behaviour, and
# RCLOL_STOP_AFTER_CLIENT_SECONDS=N to stop once the client window has been
# gone for N seconds with no game running.
exec python3 "$runner_dir/session-supervisor.py" --prefix "$prefix" \
  --darling "${darling_bin:-darling}" --runtime-at-start "$runtime_at_start" -- \
  python3 "$runner_dir/exec-guest.py" env -u ELECTRON_RUN_AS_NODE -u DYLD_INSERT_LIBRARIES EGL_PLATFORM=x11 \
  MAC_CHROMIUM_TMPDIR="${MAC_CHROMIUM_TMPDIR:-/private/tmp}" \
  LANG="${RCLOL_GUEST_LOCALE:-en_US.UTF-8}" LC_ALL="${RCLOL_GUEST_LOCALE:-en_US.UTF-8}" \
  mesa_glthread="${RCLOL_MESA_GLTHREAD-${mesa_glthread-false}}" \
  XMODIFIERS="${RCLOL_XMODIFIERS:-@im=none}" DISPLAY="${DISPLAY:-:1}" \
  DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:-$guest_path/RuntimeLibraries}" \
  DYLD_FRAMEWORK_PATH="${DYLD_FRAMEWORK_PATH:-$guest_path/RuntimeFrameworks}" \
  DARLING_XSHM_LIBRARY="${DARLING_XSHM_LIBRARY:-$guest_path/libDarlingXShm.dylib}" \
  "$darling_bin" "$A" --region="${RCLOL_REGION:-EUW}" --locale="${RCLOL_LOCALE:-en_GB}" \
    "$@" \
    --log-dir="$guest_path/Library/Logs/Riot Games/Riot Client"
