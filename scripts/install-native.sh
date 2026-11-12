#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/darling-lol-install.log"
TOOLBOX_NAME="${DARLING_LOL_TOOLBOX_NAME:-darling-lol}"
MIN_FREE_GIB=50
VERBOSE=0
USE_TOOLBOX=0
INSIDE_TOOLBOX=0
FAMILY=''
PACKAGE_MANAGER=''
PACKAGES=()
REGION=''
DEFAULT_REGION='EUW'
REGIONS=(
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
  'SEA|Southeast Asia'
  'TW|Taiwan'
  'VN|Vietnam'
  'ME|Middle East'
  'PBE|Public Beta Environment'
)
BG_PID=''
SUDO_PID=''

if [[ -f /run/.toolboxenv || ${DARLING_LOL_TOOLBOX:-0} == 1 ]]; then
  INSIDE_TOOLBOX=1
fi

if (( INSIDE_TOOLBOX )); then
  printf "Running inside Toolbox '$TOOLBOX_NAME'."
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

FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

info() {
  printf '%s▶%s %s\n' "$CYAN" "$RESET" "$*"
}

success() {
  printf '%s✔%s %s\n' "$GREEN" "$RESET" "$*"
}

warning() {
  printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2
}

fail() {
  printf '\n%s✖ ERROR:%s %s\n' "$RED$BOLD" "$RESET" "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n $BG_PID ]]; then
    kill "$BG_PID" 2>/dev/null || true
  fi
  if [[ -n $SUDO_PID ]]; then
    kill "$SUDO_PID" 2>/dev/null || true
  fi
  if [[ -t 1 ]]; then
    printf '\e[?25h'
  fi
}

trap cleanup EXIT
trap 'exit 130' INT TERM
trap 'printf "\n%s✖ Unexpected failure at line %s.%s\n" "$RED" "$LINENO" "$RESET" >&2' ERR

usage() {
  cat <<EOF
Usage: ${0##*/} [options]

Install Darling-LoL build dependencies, compile the project and install its runtime.

  -v, --verbose   Show all command output
  -t, --toolbox   Run inside a Fedora Toolbox container
  -h, --help      Show this help
EOF
}

run() {
  local description=$1
  local rc=0
  local start=$SECONDS
  local elapsed
  local frame=0

  shift
  info "$description"

  if (( VERBOSE )); then
    printf '\n%s▶ %s%s\n' "$BOLD$CYAN" "$description" "$RESET"
    "$@" || rc=$?
  else
    mkdir -p -- "$(dirname "$LOG")"
    "$@" >>"$LOG" 2>&1 &
    BG_PID=$!
    printf '\e[?25l'
    while kill -0 "$BG_PID" 2>/dev/null; do
      printf '\r%s%s%s %s' "$CYAN" "${FRAMES[frame++ % ${#FRAMES[@]}]}" "$RESET" "$description"
      sleep 0.12
    done
    wait "$BG_PID" || rc=$?
    BG_PID=''
    printf '\r\e[K\e[?25h'
  fi

  elapsed=$((SECONDS - start))

  if (( rc != 0 )); then
    printf '%s✖%s %s\n' "$RED" "$RESET" "$description" >&2
    if (( ! VERBOSE )); then
      printf '\n%sLast log lines:%s\n' "$BOLD" "$RESET" >&2
      tail -n 25 "$LOG" >&2 || true
      printf '\nFull log: %s\n' "$LOG" >&2
    fi
    fail "Step failed with exit code $rc."
  fi

  printf '%s✔%s %s %s(%dm%02ds)%s\n' \
    "$GREEN" "$RESET" "$description" "$DIM" \
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

detect_distribution() {
  [[ -r /etc/os-release ]] || fail 'Cannot identify the Linux distribution.'
  . /etc/os-release

  local ids=" ${ID:-} ${ID_LIKE:-} "
  local ver_id=${VERSION_ID:-}
  local ver_major=${ver_id%%.*}

  case "$ids" in
    *' fedora '*)
      [[ $ver_major =~ ^[0-9]+$ ]] && (( ver_major >= 43 )) ||
        fail 'Fedora 43 or newer is required (FFmpeg 8 / libavcodec ABI 62).'
      FAMILY=fedora
      ;;
    *' arch '*)
      FAMILY=arch
      ;;
    *' debian '*)
      if [[ ${ID:-} == ubuntu || $ids == *' ubuntu '* ]]; then
        [[ $ver_major =~ ^[0-9]+$ ]] && (( ver_major >= 26 )) ||
          fail 'Ubuntu 26.04 or newer is required (FFmpeg 8 / libavcodec ABI 62).'
      elif [[ $ver_major =~ ^[0-9]+$ ]]; then
        (( ver_major >= 14 )) ||
          fail 'Debian 14 (forky) or newer is required (FFmpeg 8 / libavcodec ABI 62).'
      else
        case "${VERSION_CODENAME:-}" in
          forky|sid) ;;
          *) fail 'Debian 14 (forky) or newer is required (FFmpeg 8 / libavcodec ABI 62).' ;;
        esac
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
        libcap vulkan-headers vulkan-radeon vulkan-icd-loader vulkan-tools 
        xorg-xwayland xorg-xdpyinfo xorg-xprop xorg-xwininfo xorg-xrandr 
        xdg-user-dirs desktop-file-utils
      )
      ;;
    debian)
      PACKAGE_MANAGER=apt-get
      PACKAGES=(
        build-essential cmake make ninja-build clang lld llvm-dev llvm-runtime 
        bison flex xz-utils libfuse-dev libudev-dev pkg-config libc6-dev-i386 
        libcap2-bin git git-lfs curl unzip ca-certificates python3 
        python3-venv python3-pip libglu1-mesa-dev libcairo2-dev 
        libgl1-mesa-dev libtiff-dev libfreetype6-dev libxml2-dev 
        libegl1-mesa-dev libfontconfig1-dev libbsd-dev libxrandr-dev 
        libxcursor-dev libgif-dev libpulse-dev libavformat-dev 
        libavcodec-dev libswresample-dev libdbus-1-dev libxkbfile-dev 
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

  [[ -f "$TARGET_DIR/scripts/build.sh" ]] || fail 'Run this script from a Darling-LoL repository checkout.'
}

install_toolbox() {
  [[ $FAMILY == fedora ]] || fail '--toolbox is currently supported only on Fedora hosts.'
  command -v toolbox >/dev/null 2>&1 || fail 'Toolbox is not installed.'
  command -v podman >/dev/null 2>&1 || fail 'Podman is not installed.'

  if ! podman container exists "$TOOLBOX_NAME"; then
    run "Creating Toolbox container '$TOOLBOX_NAME'" \
      toolbox create --assumeyes --distro fedora "$TOOLBOX_NAME"
    printf 'Toolbox '$TOOLBOX_NAME' created successfully.\n'
  else
    printf 'Toolbox '$TOOLBOX_NAME' already exists.\n'
  fi

  info "Entering Toolbox '$TOOLBOX_NAME'..."
  printf '\n'

  local args=()
  (( VERBOSE )) && args+=(--verbose)

  exec toolbox run --container "$TOOLBOX_NAME" \
    env DARLING_LOL_TOOLBOX=1 \
    bash "$0" "${args[@]}"
}

choose_region() {
  local index=1 entry choice

  printf '\n%s%sSelect your Riot server region:%s\n' "$BOLD" "$CYAN" "$RESET"
  for entry in "${REGIONS[@]}"; do
    printf '  %2d) %-5s %s\n' "$index" "${entry%%|*}" "${entry#*|}"
    index=$((index + 1))
  done

  while true; do
    read -r -p "Region number or code [${DEFAULT_REGION}]: " choice
    choice=${choice:-$DEFAULT_REGION}
    choice=${choice^^}
    index=1

    for entry in "${REGIONS[@]}"; do
      if [[ $choice == "${entry%%|*}" || $choice == "$index" ]]; then
        REGION=${entry%%|*}
        return
      fi
      index=$((index + 1))
    done

    warning 'Invalid region. Choose one of the listed regions.'
  done
}

confirm_arch_upgrade() {
  [[ $FAMILY == arch ]] || return 0
  printf '\n'
  warning 'Arch Linux dependency installation runs a full system upgrade (pacman -Syu).'
  printf 'This may upgrade all installed Arch packages and require manual intervention.\n'
  local reply
  read -r -p 'Proceed with full system upgrade? [y/N]: ' reply
  [[ ${reply^^} == Y || ${reply^^} == YES ]] || fail 'Installation cancelled.'
}

install_packages() {
  case "$FAMILY" in
    fedora)
      sudo dnf install -y "${PACKAGES[@]}" || return $?
      ;;
    arch)
      sudo pacman -Syu --needed -- "${PACKAGES[@]}" || return $?
      ;;
    debian)
      sudo apt-get update || return $?
      sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}" || return $?
      ;;
  esac

  git lfs install --skip-repo || return $?
}

verify_ffmpeg() {
  local version major
  version=$(pkg-config --modversion libavcodec 2>/dev/null || true)
  major=${version%%.*}
  [[ $major == 62 ]] ||
    fail "FFmpeg 8 (libavcodec ABI 62) is required; found ${version:-not installed}."
}

build_project() {
  cd "$TARGET_DIR"

  python3 -m venv build/venv
  build/venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
  python3 scripts/configure.py --guest-user "$(id -un)"
  sh scripts/build.sh
  sudo cmake --install build/darling-src/build
  sh scripts/install-runtime.sh
}

finish() {
  printf '\n%s%sDarling-LoL installed successfully.%s\n\n' "$GREEN" "$BOLD" "$RESET"
  printf 'Repository: %s\n' "$TARGET_DIR"
  printf 'Branch: %s\n' "$(git -C "$TARGET_DIR" branch --show-current)"
  printf 'Commit: %s\n\n' "$(git -C "$TARGET_DIR" rev-parse --short HEAD)"
  printf 'To start the runtime:\n'
  if (( INSIDE_TOOLBOX )); then
    printf '  toolbox enter %s\n' "$TOOLBOX_NAME"
  fi
  printf '  cd "%s"\n' "$TARGET_DIR"
  printf '  sh scripts/run.sh\n\n'
  printf 'To install Riot Client later (%s):\n' "$REGION"
  printf '  ./scripts/installer.sh %s\n\n' "${REGION,,}"
  warning 'Review the current README safety notice before launching Riot Client.'
}

for arg in "$@"; do
  case "$arg" in
    -v|--verbose) VERBOSE=1 ;;
    -t|--toolbox) USE_TOOLBOX=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "Unknown option: $arg" ;;
  esac
done

detect_distribution
preflight

if (( USE_TOOLBOX && ! INSIDE_TOOLBOX )); then
  install_toolbox
  exit 0
fi

if [[ ${DARLING_LOL_TOOLBOX:-0} != 1 ]]; then
  printf '\n%s%sDarling-LoL installer%s\n' "$BOLD" "$CYAN" "$RESET"
  printf '%sDistribution: %s%s\n' "$DIM" "$FAMILY" "$RESET"
  printf '%sBuild dependencies will be installed and Darling-LoL will be compiled from source.%s\n' "$DIM" "$RESET"
  printf '%sRiot Client region: selected interactively after confirmation.%s\n\n' "$DIM" "$RESET"
  read -r -p "Type ${BOLD}INSTALL${RESET} to continue: " confirmation
  [[ $confirmation == INSTALL ]] || fail 'Installation cancelled.'
fi

choose_region
confirm_arch_upgrade

mkdir -p -- "$(dirname "$LOG")"
printf '\n=== %s | region=%s ===\n' "$(date -Is)" "$REGION" >>"$LOG"

keep_sudo_alive
if [[ $FAMILY == arch ]]; then
  VERBOSE=1 run 'Installing build dependencies' install_packages
else
  run 'Installing build dependencies' install_packages
fi
command -v python3 >/dev/null 2>&1 || fail 'Python 3 is required.'
run 'Checking FFmpeg compatibility' verify_ffmpeg
run 'Running project preflight checks' python3 scripts/preflight.py
run 'Preparing Python environment' bash -c '
  cd "$1"
  python3 -m venv build/venv
  build/venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
' _ "$TARGET_DIR"
run 'Configuring Darling-LoL' bash -c '
  cd "$1"
  python3 scripts/configure.py --guest-user "$(id -un)"
' _ "$TARGET_DIR"
run 'Building Darling-LoL (this may take a while)' bash -c '
  cd "$1"
  sh scripts/build.sh
' _ "$TARGET_DIR"
run 'Installing Darling system components' bash -c '
  cd "$1"
  sudo cmake --install build/darling-src/build
' _ "$TARGET_DIR"
run 'Installing the user runtime' bash -c '
  cd "$1"
  sh scripts/install-runtime.sh
' _ "$TARGET_DIR"

finish