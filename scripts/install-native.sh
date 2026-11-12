#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
TARGET_DIR=$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/darling-lol-install.log"
TOOLBOX_NAME="${DARLING_LOL_TOOLBOX_NAME:-darling-lol}"

readonly MIN_FREE_GIB=50
readonly DEFAULT_REGION='EUW'
readonly -a REGIONS=(
  'EUW|Europe West'
  'EUNE|Europe Nordic & East'
  'NA|North America'
  'BR|Brazil'
  'KR|Korea'
  'JP|Japan'
  'OCE|Oceania'
  'LAN|Latin America North'
  'LAS|Latin America South'
  'TR|Turkey'
  'RU|Russia'
)

VERBOSE=0
USE_TOOLBOX=0
INSIDE_TOOLBOX=0
YES=0
FAMILY=''
PACKAGE_MANAGER=''
PACKAGES=()
REGION=''
BG_PID=''
SUDO_PID=''

if [[ -f /run/.toolboxenv || ${DARLING_LOL_TOOLBOX:-0} == 1 ]]; then
  INSIDE_TOOLBOX=1
fi

if [[ -t 1 && -z ${NO_COLOR:-} ]]; then
  BOLD=$'\e[1m'
  DIM=$'\e[2m'
  RED=$'\e[31m'
  GREEN=$'\e[32m'
  YELLOW=$'\e[33m'
  CYAN=$'\e[36m'
  RESET=$'\e[0m'
else
  BOLD='' DIM='' RED='' GREEN='' YELLOW='' CYAN='' RESET=''
fi

if [[ ${LC_ALL:-${LC_CTYPE:-${LANG:-}}} =~ [Uu][Tt][Ff]-?8 ]]; then
  ICON_INFO='▶' ICON_OK='✔' ICON_ERR='✖' ICON_WARN='!'
  FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
else
  ICON_INFO='>' ICON_OK='+' ICON_ERR='x' ICON_WARN='!'
  FRAMES=('|' '/' '-' '\')
fi

info() {
  printf '%s%s%s %s\n' "$CYAN" "$ICON_INFO" "$RESET" "$*"
}

success() {
  printf '%s%s%s %s\n' "$GREEN" "$ICON_OK" "$RESET" "$*"
}

warning() {
  printf '%s%s%s %s\n' "$YELLOW" "$ICON_WARN" "$RESET" "$*" >&2
}

fail() {
  printf '\n%s%s ERROR:%s %s\n' "$RED$BOLD" "$ICON_ERR" "$RESET" "$*" >&2
  exit 1
}

kill_tree() {
  local pid=$1 child
  if command -v pgrep >/dev/null 2>&1; then
    while IFS= read -r child; do
      [[ -n $child ]] && kill_tree "$child"
    done < <(pgrep -P "$pid" 2>/dev/null || true)
  fi
  kill "$pid" 2>/dev/null || true
}

cleanup() {
  if [[ -n $BG_PID ]]; then
    kill_tree "$BG_PID"
  fi
  if [[ -n $SUDO_PID ]]; then
    kill "$SUDO_PID" 2>/dev/null || true
  fi
  if [[ -t 1 ]]; then
    printf '\e[?25h'
  fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'printf "\n%s%s Unexpected failure at line %s: %s%s\n" "$RED" "$ICON_ERR" "$LINENO" "$BASH_COMMAND" "$RESET" >&2' ERR

region_codes() {
  local entry codes=()
  for entry in "${REGIONS[@]}"; do
    codes+=("${entry%%|*}")
  done
  printf '%s' "${codes[*]}"
}

valid_region() {
  local entry
  for entry in "${REGIONS[@]}"; do
    [[ ${entry%%|*} == "$1" ]] && return 0
  done
  return 1
}

usage() {
  cat <<EOF
Usage: ${0##*/} [options]

Install Darling-LoL build dependencies, compile the project and install its runtime.

Options:
  -v, --verbose       Show all command output
  -t, --toolbox       Run inside a Fedora Toolbox container
  -y, --yes           Skip confirmations (required for non-interactive use)
      --region CODE   Riot server region: $(region_codes)
                      (default when not interactive: ${DEFAULT_REGION})
  -h, --help          Show this help

Environment:
  DARLING_LOL_TOOLBOX_NAME   Toolbox container name (default: darling-lol)
  NO_COLOR                   Disable colored output
  XDG_CACHE_HOME             Where the install log is written
EOF
}

parse_args() {
  while (( $# )); do
    case $1 in
      -v|--verbose) VERBOSE=1 ;;
      -t|--toolbox) USE_TOOLBOX=1 ;;
      -y|--yes) YES=1 ;;
      --region)
        (( $# >= 2 )) || fail '--region requires a value.'
        REGION=${2^^}
        shift
        ;;
      --region=*)
        REGION=${1#*=}
        REGION=${REGION^^}
        ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; fail "Unknown option: $1" ;;
    esac
    shift
  done

  if [[ -n $REGION ]] && ! valid_region "$REGION"; then
    fail "Invalid region '$REGION'. Valid regions: $(region_codes)."
  fi
}

run() {
  local description=$1
  local rc=0
  local start=$SECONDS
  local elapsed
  local frame=0
  local spin=0

  shift

  if (( VERBOSE )); then
    printf '\n%s%s %s%s\n' "$BOLD$CYAN" "$ICON_INFO" "$description" "$RESET"
    "$@" || rc=$?
  else
    if [[ -t 1 ]]; then spin=1; fi
    if (( ! spin )); then info "$description"; fi

    "$@" >>"$LOG" 2>&1 &
    BG_PID=$!

    if (( spin )); then
      printf '\e[?25l'
      while kill -0 "$BG_PID" 2>/dev/null; do
        printf '\r%s%s%s %s' "$CYAN" "${FRAMES[frame++ % ${#FRAMES[@]}]}" "$RESET" "$description"
        sleep 0.12
      done
    fi

    wait "$BG_PID" || rc=$?
    BG_PID=''

    if (( spin )); then printf '\r\e[K\e[?25h'; fi
  fi

  elapsed=$((SECONDS - start))

  if (( rc != 0 )); then
    printf '%s%s%s %s\n' "$RED" "$ICON_ERR" "$RESET" "$description" >&2
    if (( ! VERBOSE )); then
      printf '\n%sLast log lines:%s\n' "$BOLD" "$RESET" >&2
      tail -n 25 "$LOG" >&2 || true
      printf '\nFull log: %s\n' "$LOG" >&2
    fi
    fail "Step failed with exit code $rc."
  fi

  printf '%s%s%s %s %s(%dm%02ds)%s\n' \
    "$GREEN" "$ICON_OK" "$RESET" "$description" "$DIM" \
    "$((elapsed / 60))" "$((elapsed % 60))" "$RESET"
}

keep_sudo_alive() {
  sudo -v || fail 'sudo authentication failed.'
  (
    while kill -0 "$$" 2>/dev/null; do
      sudo -n true 2>/dev/null || exit
      sleep 50
    done
  ) &
  SUDO_PID=$!
}

init_log() {
  mkdir -p -- "$(dirname "$LOG")"
  ( umask 077; : >>"$LOG" )
  chmod 600 -- "$LOG" 2>/dev/null || true
  printf '\n=== %s | region=%s ===\n' "$(date -Is)" "$REGION" >>"$LOG"
}

detect_distribution() {
  [[ -r /etc/os-release ]] || fail 'Cannot identify the Linux distribution.'

  local os_id os_like os_version os_codename
  IFS='|' read -r os_id os_like os_version os_codename < <(
    . /etc/os-release
    printf '%s|%s|%s|%s\n' "${ID:-}" "${ID_LIKE:-}" "${VERSION_ID:-}" "${VERSION_CODENAME:-}"
  ) || fail 'Could not read /etc/os-release.'

  local ids=" ${os_id} ${os_like} "
  local ver_major=${os_version%%.*}

  case "$ids" in
    *' fedora '*)
      [[ $ver_major =~ ^[0-9]+$ ]] && (( ver_major >= 43 )) ||
        fail 'Fedora 43 or newer is required (FFmpeg 8 or 9).'
      FAMILY=fedora
      ;;
    *' arch '*)
      FAMILY=arch
      ;;
    *' debian '*|*' ubuntu '*)
      if [[ $os_id == ubuntu ]]; then
        [[ $ver_major =~ ^[0-9]+$ ]] && (( ver_major >= 26 )) ||
          fail 'Ubuntu 26.04 or newer is required (FFmpeg 8 or 9).'
      elif [[ $os_id == debian ]]; then
        if [[ $ver_major =~ ^[0-9]+$ ]]; then
          (( ver_major >= 14 )) ||
            fail 'Debian 14 (forky) or newer is required (FFmpeg 8 or 9).'
        else
          case "$os_codename" in
            forky|sid) ;;
            *) fail 'Debian 14 (forky) or newer is required (FFmpeg 8 or 9).' ;;
          esac
        fi
      else
        warning "Derivative '${os_id}' detected: skipping the version check (FFmpeg is verified later)."
      fi
      FAMILY=debian
      ;;
    *)
      fail 'Supported distributions: Fedora 43+, Arch Linux, Debian 14+ and Ubuntu 26.04+.'
      ;;
  esac

  case "$FAMILY" in
    fedora)
      PACKAGE_MANAGER=dnf
      PACKAGES=(
        gcc gcc-c++ make cmake ninja-build clang lld llvm llvm-devel
        bison flex git git-lfs curl unzip ca-certificates
        python3 python3-pip python3-devel
        pkgconf-pkg-config glibc-devel glibc-devel.i686
        fuse-devel systemd-devel dbus-devel elfutils-libelf-devel
        cairo-devel freetype-devel libjpeg-turbo-devel fontconfig-devel
        libglvnd-devel mesa-libGL-devel mesa-libEGL-devel mesa-libGLU-devel
        libtiff-devel libxml2-devel libbsd-devel libXcursor-devel
        libXrandr-devel giflib-devel pulseaudio-libs-devel libxkbfile-devel
        openssl-devel libcap-devel vulkan-headers vulkan-loader-devel
        libcurl-devel libedit-devel libX11-devel libXext-devel
        libXrender-devel libXfixes-devel libXcomposite-devel
        libXdamage-devel libxcb-devel xcb-util-devel xprop xdpyinfo
        xwininfo xrandr vulkan-tools mesa-vulkan-drivers
        mesa-dri-drivers xorg-x11-server-Xwayland xdg-user-dirs
        libavcodec-free-devel libavformat-free-devel
        libavutil-free-devel libswresample-free-devel
      )
      ;;
    arch)
      PACKAGE_MANAGER=pacman
      PACKAGES=(
        base-devel cmake clang lld llvm flex bison icu fuse2 gcc-multilib
        lib32-gcc-libs pkgconf fontconfig cairo libtiff mesa glu libglvnd
        libbsd libxkbfile libxcursor libxext libx11 libxcb libxkbcommon
        libxrandr libxfixes ffmpeg git git-lfs curl unzip file python
        python-pip freetype2 libxml2 giflib libpulse dbus openssl libedit
        libcap vulkan-headers vulkan-icd-loader vulkan-tools
        xorg-xwayland xorg-xdpyinfo xorg-xprop xorg-xwininfo xorg-xrandr
        xdg-user-dirs desktop-file-utils
      )
      ;;
    debian)
      PACKAGE_MANAGER=apt-get
      PACKAGES=(
        build-essential cmake make ninja-build clang lld llvm-dev llvm-runtime
        bison flex xz-utils libfuse-dev libudev-dev pkg-config libc6-dev-i386
        libcap2-bin git git-lfs curl unzip ca-certificates python3 python3-dev
        python3-venv python3-pip libglu1-mesa-dev libcairo2-dev
        libgl1-mesa-dev libtiff-dev libfreetype6-dev libxml2-dev
        libegl1-mesa-dev libfontconfig1-dev libbsd-dev libxrandr-dev
        libxcursor-dev libgif-dev libpulse-dev libavformat-dev
        libavcodec-dev libavutil-dev libswresample-dev libdbus-1-dev libxkbfile-dev
        libssl-dev libvulkan-dev libcurl4-openssl-dev libedit-dev
        libelf-dev libx11-dev libxext-dev libxrender-dev libxfixes-dev
        libxcomposite-dev libxdamage-dev libxcb1-dev libxcb-shm0-dev
        libxcb-present-dev libxcb-randr0-dev libx11-xcb-dev x11-utils
        x11-xserver-utils vulkan-tools mesa-vulkan-drivers mesa-utils
        libglvnd-dev xwayland xdg-user-dirs
      )
      ;;
  esac
}

preflight() {
  [[ $EUID -ne 0 ]] || fail 'Run as a regular user, not root.'
  [[ $(uname -m) == x86_64 ]] || fail 'Only x86_64 is supported.'
  command -v sudo >/dev/null 2>&1 || fail 'sudo is required.'
  command -v git >/dev/null 2>&1 || fail 'Git is required.'

  local kernel_major kernel_minor free_kib
  IFS=. read -r kernel_major kernel_minor _ < <(uname -r)
  kernel_minor=${kernel_minor%%[!0-9]*}
  (( kernel_major > 5 || (kernel_major == 5 && ${kernel_minor:-0} >= 14) )) ||
    fail 'Linux kernel 5.14 or newer is required.'

  free_kib=$(df -Pk "$TARGET_DIR" | awk 'NR == 2 {print $4}')
  [[ $free_kib =~ ^[0-9]+$ ]] || fail 'Could not determine available disk space.'
  (( free_kib >= MIN_FREE_GIB * 1024 * 1024 )) ||
    fail "At least ${MIN_FREE_GIB} GiB of free disk space is required."

  local required_files=(
    scripts/preflight.py
    scripts/configure.py
    scripts/build.sh
    scripts/install-runtime.sh
    requirements.txt
  )
  local rel_path
  for rel_path in "${required_files[@]}"; do
    [[ -f "$TARGET_DIR/$rel_path" ]] ||
      fail "Missing '$rel_path'. Run this script from a complete Darling-LoL repository checkout."
  done
}

check_toolbox_support() {
  [[ $FAMILY == fedora ]] || fail '--toolbox is currently supported only on Fedora hosts.'
  command -v toolbox >/dev/null 2>&1 || fail 'Toolbox is not installed.'
  command -v podman >/dev/null 2>&1 || fail 'Podman is not installed.'
}

check_repo_ffmpeg() {
  local candidate='' upstream='' major=''

  case "$FAMILY" in
    fedora)
      candidate=$(dnf repoquery --latest-limit=1 --qf '%{version}' libavcodec-free-devel 2>/dev/null | head -n 1 || true)
      if [[ -z $candidate ]]; then
        candidate=$(rpm -q --qf '%{VERSION}' libavcodec-free-devel 2>/dev/null || true)
      fi
      ;;
    arch)
      candidate=$(pacman -Si ffmpeg 2>/dev/null | awk -F': ' '/^Version/ {print $2; exit}' || true)
      if [[ -z $candidate ]]; then
        candidate=$(pacman -Q ffmpeg 2>/dev/null | awk '{print $2}' || true)
      fi
      ;;
    debian)
      candidate=$(apt-cache policy libavcodec-dev 2>/dev/null | awk '/Candidate:/ {print $2; exit}' || true)
      if [[ $candidate == '(none)' ]]; then candidate=''; fi
      ;;
  esac

  if [[ -n $candidate ]]; then
    upstream=${candidate#*:}
    major=${upstream%%.*}
    if [[ $major =~ ^[0-9]+$ ]] && (( major != 8 && major != 9 )); then
      fail "Unsupported repository FFmpeg version '$candidate' (major $major). Darling-LoL requires FFmpeg 8 (ABI 62) or FFmpeg 9 (ABI 63). Update your distribution repositories before running this installer."
    fi
  fi
}

verify_ffmpeg() {
  command -v pkg-config >/dev/null 2>&1 || fail 'pkg-config is required to verify FFmpeg libraries.'

  local libs=(libavcodec libavformat libavutil libswresample)
  local lib ver major hint=''
  local avcodec_major='' avformat_major='' avutil_major='' swresample_major=''
  local summary=()

  if [[ $FAMILY == fedora ]]; then
    hint=' On Fedora, a third-party ffmpeg-devel (for example from RPM Fusion) can conflict with the *-free-devel packages.'
  fi

  for lib in "${libs[@]}"; do
    if ! pkg-config --exists "$lib" 2>/dev/null; then
      fail "Missing FFmpeg development library '$lib' (not found via pkg-config). Ensure all FFmpeg development headers for $FAMILY are installed.$hint"
    fi
    ver=$(pkg-config --modversion "$lib" 2>/dev/null || true)
    major=${ver%%.*}
    [[ $major =~ ^[0-9]+$ ]] ||
      fail "Unable to determine ABI major version for '$lib' (reported '${ver:-unknown}')."
    summary+=("$lib=$ver (ABI $major)")
    case "$lib" in
      libavcodec) avcodec_major=$major ;;
      libavformat) avformat_major=$major ;;
      libavutil) avutil_major=$major ;;
      libswresample) swresample_major=$major ;;
    esac
  done

  if [[ $avcodec_major == 62 && $avformat_major == 62 && $avutil_major == 60 && $swresample_major == 6 ]]; then
    return 0
  fi

  if [[ $avcodec_major == 63 && $avformat_major == 63 && $avutil_major == 61 && $swresample_major == 7 ]]; then
    return 0
  fi

  fail "Incompatible or mismatched FFmpeg libraries detected (${summary[*]}). Darling-LoL requires a consistent FFmpeg 8 (62/62/60/6) or FFmpeg 9 (63/63/61/7) installation. Align your FFmpeg development packages without downgrading unrelated system packages.$hint"
}

install_toolbox() {
  if ! podman container exists "$TOOLBOX_NAME"; then
    run "Creating Toolbox container '$TOOLBOX_NAME'" \
      toolbox create --assumeyes --distro fedora "$TOOLBOX_NAME"
  else
    info "Toolbox '$TOOLBOX_NAME' already exists."
  fi

  info "Entering Toolbox '$TOOLBOX_NAME'..."
  printf '\n'

  local args=(--yes --region "$REGION")
  if (( VERBOSE )); then args+=(--verbose); fi

  exec toolbox run --container "$TOOLBOX_NAME" \
    env DARLING_LOL_TOOLBOX=1 DARLING_LOL_TOOLBOX_NAME="$TOOLBOX_NAME" \
    bash "$SCRIPT_PATH" "${args[@]}"
}

show_banner() {
  local region_note
  if [[ -n $REGION ]]; then
    region_note=$REGION
  elif (( YES )); then
    region_note="$DEFAULT_REGION (default)"
  else
    region_note='selected interactively after confirmation'
  fi

  printf '\n%s%sDarling-LoL installer%s\n' "$BOLD" "$CYAN" "$RESET"
  printf '%sDistribution: %s%s\n' "$DIM" "$FAMILY" "$RESET"
  printf '%sBuild dependencies will be installed and Darling-LoL will be compiled from source.%s\n' "$DIM" "$RESET"
  printf '%sRiot Client region: %s%s\n\n' "$DIM" "$region_note" "$RESET"
}

confirm_install() {
  if [[ ${DARLING_LOL_TOOLBOX:-0} != 1 ]]; then
    show_banner
  fi

  if (( YES )); then
    return 0
  fi

  [[ -t 0 ]] || fail 'No interactive terminal available. Re-run with --yes to confirm non-interactively.'

  local confirmation
  read -r -p "Type ${BOLD}INSTALL${RESET} to continue: " confirmation ||
    fail 'Installation cancelled.'
  [[ $confirmation == INSTALL ]] || fail 'Installation cancelled.'
}

choose_region() {
  local index=1 entry choice

  printf '\n%s%sSelect your Riot server region:%s\n' "$BOLD" "$CYAN" "$RESET"
  for entry in "${REGIONS[@]}"; do
    printf '  %2d) %-5s %s\n' "$index" "${entry%%|*}" "${entry#*|}"
    index=$((index + 1))
  done

  while true; do
    read -r -p "Region number or code [${DEFAULT_REGION}]: " choice ||
      fail 'No input received while selecting the region.'
    choice=${choice:-$DEFAULT_REGION}
    choice=${choice^^}
    index=1

    for entry in "${REGIONS[@]}"; do
      if [[ $choice == "${entry%%|*}" || $choice == "$index" ]]; then
        REGION=${entry%%|*}
        return 0
      fi
      index=$((index + 1))
    done

    warning 'Invalid region. Choose one of the listed regions.'
  done
}

resolve_region() {
  if [[ -n $REGION ]]; then
    return 0
  fi
  if (( YES )) || [[ ! -t 0 ]]; then
    REGION=$DEFAULT_REGION
  else
    choose_region
  fi
}

confirm_arch_upgrade() {
  [[ $FAMILY == arch ]] || return 0
  if (( YES )); then
    return 0
  fi

  printf '\n'
  warning 'Arch Linux dependency installation runs a full system upgrade (pacman -Syu).'
  printf 'This may upgrade all installed Arch packages and require manual intervention.\n'
  local reply
  read -r -p 'Proceed with full system upgrade? [y/N]: ' reply ||
    fail 'Installation cancelled.'
  [[ ${reply^^} == Y || ${reply^^} == YES ]] || fail 'Installation cancelled.'
}

install_packages() {
  case "$FAMILY" in
    fedora)
      sudo dnf install -y "${PACKAGES[@]}" || return $?
      ;;
    arch)
      local pacman_opts=(-Syu --needed)
      if (( YES )); then pacman_opts+=(--noconfirm); fi
      sudo pacman "${pacman_opts[@]}" -- "${PACKAGES[@]}" || return $?
      ;;
    debian)
      sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}" || return $?
      ;;
  esac

  git lfs install --skip-repo
}

prepare_python_env() {
  cd "$TARGET_DIR" &&
    python3 -m venv build/venv &&
    build/venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
}

configure_project() {
  cd "$TARGET_DIR" &&
    python3 scripts/configure.py --guest-user "$(id -un)"
}

compile_project() {
  cd "$TARGET_DIR" &&
    sh scripts/build.sh
}

install_system_components() {
  cd "$TARGET_DIR" &&
    sudo cmake --install build/darling-src/build
}

install_user_runtime() {
  cd "$TARGET_DIR" &&
    sh scripts/install-runtime.sh
}

finish() {
  local branch commit
  branch=$(git -C "$TARGET_DIR" branch --show-current 2>/dev/null || true)
  commit=$(git -C "$TARGET_DIR" rev-parse --short HEAD 2>/dev/null || true)

  printf '\n%s%sDarling-LoL installed successfully%s %s(total %dm%02ds)%s\n\n' \
    "$GREEN" "$BOLD" "$RESET" "$DIM" "$((SECONDS / 60))" "$((SECONDS % 60))" "$RESET"
  printf 'Repository: %s\n' "$TARGET_DIR"
  printf 'Branch: %s\n' "${branch:-detached HEAD}"
  printf 'Commit: %s\n' "${commit:-unknown}"
  printf 'Log: %s\n\n' "$LOG"
  printf 'To start the runtime:\n'
  if (( INSIDE_TOOLBOX )); then
    printf '  toolbox enter %s\n' "$TOOLBOX_NAME"
  fi
  printf '  cd "%s"\n' "$TARGET_DIR"
  printf '  sh scripts/run.sh\n\n'
  printf 'To install Riot Client later (%s):\n' "$REGION"
  printf '  ./scripts/installer.sh %s\n\n' "${REGION,,}"
  if [[ -f $TARGET_DIR/README.md ]]; then
    warning "Read the safety notice in $TARGET_DIR/README.md before launching Riot Client."
  else
    warning 'Read the project safety notice before launching Riot Client.'
  fi
}

parse_args "$@"

if (( INSIDE_TOOLBOX )); then
  info "Running inside Toolbox '$TOOLBOX_NAME'."
fi

detect_distribution
preflight

if (( USE_TOOLBOX && ! INSIDE_TOOLBOX )); then
  check_toolbox_support
fi

confirm_install
resolve_region
confirm_arch_upgrade
init_log

if (( USE_TOOLBOX && ! INSIDE_TOOLBOX )); then
  install_toolbox
fi

keep_sudo_alive

if [[ $FAMILY == debian ]]; then
  run 'Updating package index' sudo apt-get update
fi
check_repo_ffmpeg

if [[ $FAMILY == arch ]] && (( ! YES )); then
  prev_verbose=$VERBOSE
  VERBOSE=1
  run 'Installing build dependencies' install_packages
  VERBOSE=$prev_verbose
else
  run 'Installing build dependencies' install_packages
fi

command -v python3 >/dev/null 2>&1 || fail 'Python 3 is required.'
run 'Checking FFmpeg compatibility' verify_ffmpeg
run 'Running project preflight checks' python3 "$TARGET_DIR/scripts/preflight.py"
run 'Preparing Python environment' prepare_python_env
run 'Configuring Darling-LoL' configure_project
run 'Building Darling-LoL (this may take a while)' compile_project
run 'Installing Darling system components' install_system_components
run 'Installing the user runtime' install_user_runtime

finish