#!/usr/bin/env bash

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
TARGET_DIR=$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/darling-lol-install.log"
TOOLBOX_NAME="${DARLING_LOL_TOOLBOX_NAME:-darling-lol}"

readonly MIN_FREE_GIB=50
readonly MIN_FREE_GIB_CONTAINER=5
# Minimum supported Fedora, also used as the toolbox image release on hosts
# that cannot infer one (toolbox errors with "option '--release' is needed"
# when the requested distribution does not match the host).
readonly DEFAULT_FEDORA_RELEASE=44
readonly DEFAULT_REGION='EUW'
readonly DEFAULT_LOCALE='en_GB'
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
# Locales the Riot Client ships; keep in sync with LOCALES in scripts/common.py
# (tests/test_install_native.py compares the two lists).
readonly -a LOCALES=(
  ar_AE cs_CZ de_DE el_GR en_AU en_GB en_PH en_SG en_US es_AR es_ES es_MX
  fr_FR hu_HU id_ID it_IT ja_JP ko_KR pl_PL pt_BR ro_RO ru_RU th_TH tr_TR
  vi_VN zh_CN zh_MY zh_TW
)

VERBOSE=0
USE_TOOLBOX=0
CONTAINER_INSTALL=0
INSIDE_TOOLBOX=0
TOOLBOX_RELEASE=''
# 1 when the hidden League/Riot Client window entries were requested
# (--window-entries or DARLING_LOL_WINDOW_ENTRIES=1); never created by default.
WINDOW_ENTRIES=0
if [[ ${DARLING_LOL_WINDOW_ENTRIES:-0} == 1 ]]; then WINDOW_ENTRIES=1; fi
YES=0
NO_CONFIRM=0
FAMILY=''
PACKAGE_MANAGER=''
PACKAGES=()
REGION=''
LOCALE=''
# 1 when the locale came from --locale or RCLOL_LOCALE. Only such a locale may
# pick the region without asking; the system locale is never used unattended.
LOCALE_EXPLICIT=0
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

normalize_locale() {
  local value=${1//-/_}
  if [[ $value =~ ^([A-Za-z]{2})_([A-Za-z]{2})$ ]]; then
    printf '%s_%s' "${BASH_REMATCH[1],,}" "${BASH_REMATCH[2]^^}"
    return 0
  fi
  return 1
}

locale_codes() {
  printf '%s' "${LOCALES[*]}"
}

valid_locale() {
  local entry
  for entry in "${LOCALES[@]}"; do
    [[ $entry == "$1" ]] && return 0
  done
  return 1
}

# Normalize a locale the user gave and refuse one the Riot Client does not
# ship. $1 names the source (--locale, RCLOL_LOCALE) for the message.
explicit_locale() {
  local source=$1 value=$2 normalized
  normalized=$(normalize_locale "$value") ||
    fail "Invalid $source '$value'. Expected a value like pt_BR."
  valid_locale "$normalized" ||
    fail "Unsupported $source '$value'. Supported locales: $(locale_codes)."
  printf '%s' "$normalized"
}

# The system locale when the Riot Client ships it; fails otherwise (fr_CA,
# de_AT, C, ...), so callers fall back to the default.
supported_system_locale() {
  local detected
  detected=$(detect_system_locale) || return 1
  valid_locale "$detected" || return 1
  printf '%s' "$detected"
}

interactive_run() {
  (( ! YES )) && [[ -t 0 ]]
}

detect_system_locale() {
  local name value
  for name in LC_ALL LC_MESSAGES LANG; do
    value=${!name:-}
    [[ -n $value ]] || continue
    value=${value%%.*}
    value=${value%%@*}
    if normalize_locale "$value"; then
      return 0
    fi
    # The first non-empty variable wins, even when it cannot be parsed.
    return 1
  done
  return 1
}

region_for_country() {
  # Map a locale's country part to a Riot region; unknown countries fail, so
  # callers can tell a real match apart from the generic default.
  local country=${1:-}

  case "$country" in
    BR) printf 'BR' ;;
    US|CA) printf 'NA' ;;
    MX|GT|BZ|SV|HN|NI|CR|PA|DO|PR|CU|HT|JM|TT|BB|BS) printf 'LAN' ;;
    AR|BO|CL|CO|EC|PE|PY|UY|VE|GY|SR|GF) printf 'LAS' ;;
    KR) printf 'KR' ;;
    JP) printf 'JP' ;;
    TR) printf 'TR' ;;
    RU|BY|KZ) printf 'RU' ;;
    AU|NZ|FJ|PG) printf 'OCE' ;;
    PL|CZ|SK|HU|RO|BG|HR|SI|RS|BA|MK|AL|ME|EE|LV|LT|UA|MD|SE|NO|DK|FI|IS) printf 'EUNE' ;;
    *) return 1 ;;
  esac
}

region_for_locale() {
  local locale=${1:-}
  local country='' region

  [[ $locale == *_* ]] && country=${locale#*_}

  if region=$(region_for_country "$country"); then
    printf '%s' "$region"
    return 0
  fi
  printf '%s' "$DEFAULT_REGION"
}

usage() {
  cat <<EOF
Usage: ${0##*/} [options]

Install Darling-LoL build dependencies, compile the project and install its runtime.

Options:
  -v, --verbose       Show all command output
  -t, --toolbox       Build inside a Fedora Toolbox container (any host with toolbox and podman)
  -y, --yes           Skip confirmations (required for non-interactive use)
      --no-confirm     Skip the INSTALL confirmation only (used by the Toolbox re-exec; does not imply --yes)
      --region CODE   Riot server region: $(region_codes)
                      (asked interactively; with --yes or without a terminal:
                      the region of an explicit locale, else ${DEFAULT_REGION})
      --locale CODE   Riot client locale for new installs, for example pt_BR
                      (asked interactively, with the system locale suggested;
                      with --yes or without a terminal: ${DEFAULT_LOCALE})
                      A prefix that already ran the client keeps its saved
                      language unless RCLOL_LOCALE is exported at launch.
      --window-entries  Also create hidden desktop entries that name and icon the
                      League and Riot Client windows in the GNOME overview
                      (off by default; the launcher itself stays neutral)
  -h, --help          Show this help

Environment:
  RCLOL_REGION               Riot server region, same as --region
  RCLOL_LOCALE               Riot client locale, same as --locale
  DARLING_LOL_TOOLBOX_NAME   Toolbox container name (default: darling-lol)
  DARLING_LOL_TOOLBOX_RELEASE  Fedora release for 'toolbox create' on non-Fedora
                             hosts (default: ${DEFAULT_FEDORA_RELEASE})
  DARLING_LOL_DESKTOP_ENTRY  Create the desktop entry without asking (1/0);
                             when unset, ask at the end of an interactive install
  DARLING_LOL_WINDOW_ENTRIES Same as --window-entries when set to 1
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
      --window-entries) WINDOW_ENTRIES=1 ;;
      --no-confirm) NO_CONFIRM=1 ;;
      --region)
        (( $# >= 2 )) || fail '--region requires a value.'
        REGION=${2^^}
        shift
        ;;
      --region=*)
        REGION=${1#*=}
        REGION=${REGION^^}
        ;;
      --locale)
        (( $# >= 2 )) || fail '--locale requires a value.'
        LOCALE=$(explicit_locale locale "$2") || exit 1
        LOCALE_EXPLICIT=1
        shift
        ;;
      --locale=*)
        LOCALE=$(explicit_locale locale "${1#*=}") || exit 1
        LOCALE_EXPLICIT=1
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
  printf '\n=== %s | region=%s | locale=%s ===\n' "$(date -Is)" "$REGION" "$LOCALE" >>"$LOG"
}

detect_distribution() {
  # DARLING_OS_RELEASE is a test seam; production always reads /etc/os-release.
  local os_release=${DARLING_OS_RELEASE:-/etc/os-release}
  [[ -r $os_release ]] || fail 'Cannot identify the Linux distribution.'

  local os_id os_like os_version os_codename
  IFS='|' read -r os_id os_like os_version os_codename < <(
    . "$os_release"
    printf '%s|%s|%s|%s\n' "${ID:-}" "${ID_LIKE:-}" "${VERSION_ID:-}" "${VERSION_CODENAME:-}"
  ) || fail 'Could not read /etc/os-release.'

  local ids=" ${os_id} ${os_like} "
  local ver_major=${os_version%%.*}

  if (( CONTAINER_INSTALL )); then
    # The host only launches the Toolbox container: the distribution and
    # FFmpeg requirements are enforced inside the container, so the host
    # identity is informational and never gates the install.
    case "$ids" in
      *' fedora '*) FAMILY=fedora ;;
      *' arch '*) FAMILY=arch ;;
      *' debian '*|*' ubuntu '*) FAMILY=debian ;;
      *) FAMILY=other ;;
    esac
    # Effective Toolbox image release: explicit override, then the host's own
    # Fedora release when it already meets the minimum, then the minimum
    # supported one. The banner shows the release that will be created, so an
    # old Fedora host is bumped up instead of showing its unsupported version.
    if [[ -n ${DARLING_LOL_TOOLBOX_RELEASE:-} ]]; then
      TOOLBOX_RELEASE=$DARLING_LOL_TOOLBOX_RELEASE
    elif [[ $FAMILY == fedora && $ver_major =~ ^[0-9]+$ ]] &&
         (( ver_major >= DEFAULT_FEDORA_RELEASE )); then
      TOOLBOX_RELEASE=$ver_major
    else
      TOOLBOX_RELEASE=$DEFAULT_FEDORA_RELEASE
    fi
    return 0
  fi

  case "$ids" in
    *' fedora '*)
      [[ $ver_major =~ ^[0-9]+$ ]] && (( ver_major >= 44 )) ||
        fail 'Fedora 44 or newer is required (FFmpeg 8 or 9).'
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
      fail 'Supported distributions: Fedora 44+, Arch Linux, Debian 14+ and Ubuntu 26.04+.'
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
  # Container installs never run a package manager on the host; sudo is only
  # needed inside the Toolbox, where it is passwordless.
  if [[ $CONTAINER_INSTALL == 0 ]]; then
    command -v sudo >/dev/null 2>&1 || fail 'sudo is required.'
  fi
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

check_podman_storage_space() {
  local graph_root probe free_kib
  if ! graph_root=$(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null) || [[ -z $graph_root ]]; then
    graph_root="${XDG_DATA_HOME:-$HOME/.local/share}/containers"
  fi

  # df needs an existing path; walk up to the nearest existing ancestor.
  probe=$graph_root
  until [[ -d $probe || $probe == / ]]; do
    probe=$(dirname -- "$probe")
  done

  free_kib=$(df -Pk "$probe" 2>/dev/null | awk 'NR == 2 {print $4}')
  [[ $free_kib =~ ^[0-9]+$ ]] ||
    fail "Could not determine available disk space for Podman storage '$graph_root'."
  (( free_kib >= MIN_FREE_GIB_CONTAINER * 1024 * 1024 )) ||
    fail "Podman storage '$graph_root' is on a filesystem with less than ${MIN_FREE_GIB_CONTAINER} GiB free. Container images live outside the repository checkout; free space there before installing."
}

check_toolbox_support() {
  # Package-manager hint follows the host distribution identified by
  # detect_distribution; unset/unknown families get a generic fallback.
  local hint
  case "$FAMILY" in
    fedora) hint='sudo dnf install toolbox podman' ;;
    arch) hint='sudo pacman -S podman toolbox' ;;
    debian) hint='sudo apt install podman-toolbox' ;;
    *) hint='the toolbox and podman packages from your distribution' ;;
  esac
  command -v toolbox >/dev/null 2>&1 ||
    fail "Toolbox is not installed. Install it with '${hint}'."
  command -v podman >/dev/null 2>&1 ||
    fail "Podman is not installed. Install it with '${hint}'."
  check_podman_storage_space
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
  local create_args=(--assumeyes --distro fedora)
  # TOOLBOX_RELEASE is resolved in detect_distribution (override > host Fedora
  # release when it meets the minimum > minimum supported). The env override
  # is honored here too so the create call stays self-contained.
  create_args+=(--release "${DARLING_LOL_TOOLBOX_RELEASE:-${TOOLBOX_RELEASE:-$DEFAULT_FEDORA_RELEASE}}")

  if ! podman container exists "$TOOLBOX_NAME"; then
    run "Creating Toolbox container '$TOOLBOX_NAME'" \
      toolbox create "${create_args[@]}" "$TOOLBOX_NAME"
  else
    info "Toolbox '$TOOLBOX_NAME' already exists."
  fi

  info "Entering Toolbox '$TOOLBOX_NAME'..."
  printf '\n'

  local args=(--no-confirm --region "$REGION" --locale "$LOCALE")
  if (( YES )); then args+=(--yes); fi
  if (( VERBOSE )); then args+=(--verbose); fi
  if (( WINDOW_ENTRIES )); then args+=(--window-entries); fi

  # Forward the desktop-entry choice only when set: the inner installer treats
  # unset as "prompt" and 0 as "suppress the prompt without creating an entry".
  local inner_env=(env DARLING_LOL_TOOLBOX=1 DARLING_LOL_TOOLBOX_NAME="$TOOLBOX_NAME")
  if [[ -n ${DARLING_LOL_DESKTOP_ENTRY+x} ]]; then
    inner_env+=(DARLING_LOL_DESKTOP_ENTRY="$DARLING_LOL_DESKTOP_ENTRY")
  fi

  exec toolbox run --container "$TOOLBOX_NAME" \
    "${inner_env[@]}" bash "$SCRIPT_PATH" "${args[@]}"
}

show_banner() {
  local region_note locale_note distribution
  if [[ -n $REGION ]]; then
    region_note=$REGION
  elif [[ -n ${RCLOL_REGION:-} ]]; then
    region_note=${RCLOL_REGION^^}
  elif (( LOCALE_EXPLICIT )); then
    region_note="$(region_for_locale "$LOCALE")"
  elif ! interactive_run; then
    region_note="$DEFAULT_REGION (default)"
  else
    region_note='selected interactively after confirmation'
  fi

  if (( LOCALE_EXPLICIT )); then
    locale_note=$LOCALE
  elif [[ -n $LOCALE ]]; then
    locale_note="$LOCALE (default)"
  else
    locale_note='selected interactively after confirmation'
  fi

  distribution="Distribution: $FAMILY"
  if (( CONTAINER_INSTALL )); then
    distribution="Toolbox Distribution: Fedora ${TOOLBOX_RELEASE:-$DEFAULT_FEDORA_RELEASE}"
  fi

  printf '\n%s%sDarling-LoL installer%s\n' "$BOLD" "$CYAN" "$RESET"
  printf '%s%s%s\n' "$DIM" "$distribution" "$RESET"
  printf '%sRiot Client region: %s%s\n' "$DIM" "$region_note" "$RESET"
  printf '%sRiot Client locale: %s%s\n\n' "$DIM" "$locale_note" "$RESET"
  printf '%sBuild dependencies will be installed and Darling-LoL will be compiled from source.%s\n\n' "$DIM" "$RESET"
}

confirm_install() {
  if [[ ${DARLING_LOL_TOOLBOX:-0} != 1 ]]; then
    show_banner
  fi

  if (( YES || NO_CONFIRM )); then
    return 0
  fi

  [[ -t 0 ]] || fail 'No interactive terminal available. Re-run with --yes to confirm non-interactively.'

  local confirmation
  read -r -p "${CYAN}${ICON_INFO}${RESET} Type ${BOLD}INSTALL${RESET} to continue: " confirmation ||
    fail 'Installation cancelled.'
  [[ $confirmation == INSTALL ]] || fail 'Installation cancelled.'
}

choose_region() {
  local default_region=${1:-$DEFAULT_REGION}
  local index=1 entry choice

  printf '\n%s%sSelect your Riot server region:%s\n' "$BOLD" "$CYAN" "$RESET"
  for entry in "${REGIONS[@]}"; do
    printf '  %2d) %-5s %s\n' "$index" "${entry%%|*}" "${entry#*|}"
    index=$((index + 1))
  done

  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Region number or code [${default_region}]: " choice ||
      fail 'No input received while selecting the region.'
    choice=${choice:-$default_region}
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

# Runs before the INSTALL confirmation. An explicit locale wins; an unattended
# run keeps the default and never reads the system locale. An interactive run
# leaves LOCALE empty for select_locale.
resolve_locale() {
  if [[ -n $LOCALE ]]; then
    return 0
  fi
  if [[ -n ${RCLOL_LOCALE:-} ]]; then
    LOCALE=$(explicit_locale RCLOL_LOCALE "$RCLOL_LOCALE") || exit 1
    LOCALE_EXPLICIT=1
    return 0
  fi
  if ! interactive_run; then
    LOCALE=$DEFAULT_LOCALE
  fi
}

choose_locale() {
  local default_locale=${1:-$DEFAULT_LOCALE}
  local choice normalized

  printf '\n%s%sSelect the Riot Client language:%s\n' "$BOLD" "$CYAN" "$RESET"
  local index line=' '
  for index in "${!LOCALES[@]}"; do
    line+=" ${LOCALES[index]}"
    if (( (index + 1) % 7 == 0 )); then
      printf '%s\n' "$line"
      line=' '
    fi
  done
  [[ $line == ' ' ]] || printf '%s\n' "$line"
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Locale [${default_locale}]: " choice ||
      fail 'No input received while selecting the locale.'
    choice=${choice:-$default_locale}
    if normalized=$(normalize_locale "$choice") && valid_locale "$normalized"; then
      LOCALE=$normalized
      return 0
    fi
    warning 'Unsupported locale. Choose one of the listed locales.'
  done
}

# Interactive runs only: suggest the system locale when the Riot Client ships
# it, and let the user confirm or change it.
select_locale() {
  local suggested
  if [[ -n $LOCALE ]]; then
    return 0
  fi
  if ! interactive_run; then
    LOCALE=$DEFAULT_LOCALE
    return 0
  fi
  suggested=$(supported_system_locale) || suggested=$DEFAULT_LOCALE
  choose_locale "$suggested"
}

resolve_region() {
  local detected reply
  if [[ -n $REGION ]]; then
    return 0
  fi
  if [[ -n ${RCLOL_REGION:-} ]]; then
    REGION=${RCLOL_REGION^^}
    valid_region "$REGION" ||
      fail "Invalid RCLOL_REGION '$RCLOL_REGION'. Valid regions: $(region_codes)."
    return 0
  fi

  if ! interactive_run; then
    # Unattended: only a locale the user gave may pick the region.
    if (( LOCALE_EXPLICIT )); then
      REGION=$(region_for_locale "$LOCALE")
    else
      REGION=$DEFAULT_REGION
    fi
    return 0
  fi
  detected=$(region_for_locale "$LOCALE")

  # When the chosen locale maps to a server, offer it as a one-key shortcut
  # before falling back to the manual region list. The redirect keeps
  # region_for_country's stdout out of the terminal (it runs outside $(...)).
  if [[ $LOCALE == *_* ]] && region_for_country "${LOCALE#*_}" >/dev/null; then
    while true; do
      read -r -p "${CYAN}${ICON_INFO}${RESET} Use the region server for your locale (${detected})? [Y/n]: " reply ||
        fail 'No input received while selecting the region.'
      reply=${reply,,}
      case "$reply" in
        ''|y|yes)
          REGION=$detected
          return 0
          ;;
        n|no)
          break
          ;;
        *)
          warning 'Invalid answer. Enter y or n.'
          ;;
      esac
    done
  fi

  choose_region "$detected"
}

confirm_arch_upgrade() {
  # Nothing is installed on the host in container mode; the pacman upgrade
  # warning only applies when the host itself runs the build.
  [[ $CONTAINER_INSTALL == 0 ]] || return 0
  [[ $FAMILY == arch ]] || return 0
  if (( YES )); then
    return 0
  fi

  printf '\n'
  warning 'Arch Linux dependency installation runs a full system upgrade (pacman -Syu).'
  printf 'This may upgrade all installed Arch packages and require manual intervention.\n'
  local reply
  read -r -p "${CYAN}${ICON_INFO}${RESET} Proceed with full system upgrade? [y/N]: " reply ||
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

install_packages_tee() {
  # Log the interactive pacman output as well: 'run' with VERBOSE=1 prints to
  # the terminal only, and pipefail propagates install_packages' exit code.
  install_packages 2>&1 | tee -a "$LOG"
}

prepare_python_env() {
  cd "$TARGET_DIR" &&
    python3 -m venv build/venv &&
    build/venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
}

configure_project() {
  cd "$TARGET_DIR" &&
    python3 scripts/configure.py \
      --guest-user "$(id -un)" \
      --region "$REGION" \
      --locale "$LOCALE"
}

compile_project() {
  cd "$TARGET_DIR" &&
    sh scripts/build.sh
}

ensure_no_active_session() {
  local rc=0
  python3 - <<'PY' || rc=$?
from pathlib import Path
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        command_line = (entry / 'cmdline').read_bytes()
    except OSError:
        continue
    if b'/Riot Client.app/' in command_line or b'/League of Legends.app/' in command_line:
        raise SystemExit(3)
PY
  case $rc in
    0) ;;
    3) fail 'A Riot Client or League process is running; exit the game and both clients, then run this again.' ;;
    *) fail "Could not check for running Riot/League processes (exit code $rc)." ;;
  esac
}

install_system_components() {
  cd "$TARGET_DIR" &&
    sudo cmake --install build/darling-src/build
}

install_user_runtime() {
  cd "$TARGET_DIR" &&
    sh scripts/install-runtime.sh
}

launcher_entry_uses_shortcut_script() {
  # 0 when the installed menu entry exists and already targets the
  # auto-installing launcher script. A missing entry is not ready, so the
  # manual runtime instructions must stay visible.
  local entry="${HOME:-}/.local/share/applications/darling-launcher.desktop"
  [[ -f $entry ]] && grep -q 'scripts/launch-shortcut.sh' "$entry"
}

create_desktop_entry() {
  local helper="$TARGET_DIR/build/runtime/tools/desktop-entry.py"
  local args=()

  if [[ ! -f $helper ]]; then
    warning "Desktop entry helper not found at $helper; skipping."
    return 0
  fi

  if (( INSIDE_TOOLBOX )); then
    args+=(--toolbox "$TOOLBOX_NAME")
  fi
  if (( WINDOW_ENTRIES )); then
    args+=(--window-entries)
  fi

  local entry="${HOME:-}/.local/share/applications/darling-launcher.desktop"
  if [[ -f $entry ]] && ! grep -q 'scripts/launch-shortcut.sh' "$entry"; then
    info 'Updating the Darling Launcher shortcut to use scripts/launch-shortcut.sh (Riot Client auto-installer).'
  fi

  # Re-emit the helper's report in the dim summary color; stderr passes
  # through untouched, and the helper's exit status decides the warning.
  local helper_output='' helper_rc=0 line
  helper_output=$(python3 "$helper" "${args[@]}") || helper_rc=$?

  if [[ -n $helper_output ]]; then
    while IFS= read -r line; do
      printf '%s%s%s\n' "$DIM" "$line" "$RESET"
    done <<<"$helper_output"
  fi

  if (( helper_rc != 0 )); then
    warning 'Could not create the desktop entry. Run scripts/create-desktop-entry.sh afterwards.'
  elif [[ -f $entry ]] && ! grep -q 'scripts/launch-shortcut.sh' "$entry"; then
    warning 'The Darling Launcher shortcut is still outdated; re-run scripts/create-desktop-entry.sh to finish updating it.'
  fi
}

offer_desktop_entry() {
  local choice=${DARLING_LOL_DESKTOP_ENTRY:-}

  case "${choice,,}" in
    1|yes|true)
      create_desktop_entry
      # Ready means the installed entry targets the auto-installer; a failed
      # or outdated creation keeps the manual instructions in the summary.
      launcher_entry_uses_shortcut_script
      return
      ;;
    0|no|false)
      return 1
      ;;
  esac

  if (( YES )) || [[ ! -t 0 ]]; then
    return 1
  fi

  printf '\n'
  local reply
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Create a desktop entry automatically? [Y/n]: " reply || return 1
    case "${reply,,}" in
      ''|y|yes)
        create_desktop_entry
        # Ready means the installed entry targets the auto-installer; a
        # failed or outdated creation keeps the manual instructions.
        launcher_entry_uses_shortcut_script
        return
        ;;
      n|no)
        return 1
        ;;
      *)
        warning 'Invalid answer. Enter y or n.'
        ;;
    esac
  done
}

offer_riot_client_installer() {
  if (( YES )) || [[ ! -t 0 ]]; then
    return 1
  fi
  if [[ ! -f $TARGET_DIR/scripts/installer.sh ]]; then
    return 1
  fi

  # The runtime refuses to start before the Riot Client is installed, so
  # offer to run the installer right away. It stays in the foreground for the
  # whole client session, so the default answer is no.
  local region=${REGION:-$DEFAULT_REGION} locale=${LOCALE:-$DEFAULT_LOCALE}
  printf '\n'
  local reply
  read -r -p "${CYAN}${ICON_INFO}${RESET} Launch the Riot Client installer now? [y/N]: " reply || return 1
  reply=${reply,,}
  case "$reply" in
    y|yes) ;;
    *) return 1 ;;
  esac

  # installer.sh takes the region as its argument and the locale from the
  # environment (run-riot-client.sh reads RCLOL_LOCALE).
  (
    cd "$TARGET_DIR" &&
      RCLOL_LOCALE="$locale" ./scripts/installer.sh "${region,,}"
  )
}

finish() {
  local branch commit region=${REGION:-$DEFAULT_REGION}
  branch=$(git -C "$TARGET_DIR" branch --show-current 2>/dev/null || true)
  commit=$(git -C "$TARGET_DIR" rev-parse --short HEAD 2>/dev/null || true)

  printf '\n%s%sDarling-LoL installed successfully%s %s(total %dm%02ds)%s\n\n' \
    "$GREEN" "$BOLD" "$RESET" "$DIM" "$((SECONDS / 60))" "$((SECONDS % 60))" "$RESET"
  printf '%sRepository: %s%s\n' "$DIM" "$TARGET_DIR" "$RESET"
  printf '%sBranch: %s%s\n' "$DIM" "${branch:-detached HEAD}" "$RESET"
  printf '%sCommit: %s%s\n' "$DIM" "${commit:-unknown}" "$RESET"
  printf '%sRegion/locale: %s / %s (used for new installs and the first launch)%s\n' \
    "$DIM" "${REGION:-$DEFAULT_REGION}" "${LOCALE:-$DEFAULT_LOCALE}" "$RESET"
  printf '%sLog: %s%s\n\n' "$DIM" "$LOG" "$RESET"
  local shortcut_ready=0
  if offer_desktop_entry; then
    shortcut_ready=1
  fi

  # The notice comes before the offer below: an accepted offer starts Riot
  # Client and only returns when that session ends.
  if [[ -f $TARGET_DIR/README.md ]]; then
    warning "Read the safety notice in $TARGET_DIR/README.md before launching Riot Client."
  else
    warning 'Read the project safety notice before launching Riot Client.'
  fi

  local riot_launched=0
  if offer_riot_client_installer; then
    riot_launched=1
  fi

  # With the launcher shortcut in place the entry script handles both the
  # launch and the Riot Client auto-install, so the manual instructions
  # would only repeat what the shortcut already does.
  if (( ! shortcut_ready )); then
    printf 'To start the runtime:\n'
    if (( INSIDE_TOOLBOX )); then
      printf '  toolbox enter %s\n' "$TOOLBOX_NAME"
    fi
    printf '  cd "%s"\n' "$TARGET_DIR"
    printf '  RCLOL_REGION=%s RCLOL_LOCALE=%s sh scripts/run.sh\n\n' \
      "${REGION:-$DEFAULT_REGION}" "${LOCALE:-$DEFAULT_LOCALE}"
  fi
  if (( ! riot_launched && ! shortcut_ready )); then
    printf 'To install Riot Client later (%s):\n' "$region"
    printf '  ./scripts/installer.sh %s\n\n' "${region,,}"
  fi

  # One-line pointer to the interactive shortcut manager: renaming the
  # shortcut, the window entries, launch flags, a Toolbox or host target and
  # a custom icon (custom-icon.* via --icon).
  if (( shortcut_ready )); then printf '\n'; fi
  printf 'To manage the shortcuts and icons later:\n'
  printf '  ./scripts/create-shortcuts.sh\n'
}

main() {
  set -Eeuo pipefail
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'printf "\n%s%s Unexpected failure at line %s: %s%s\n" "$RED" "$ICON_ERR" "$LINENO" "$BASH_COMMAND" "$RESET" >&2' ERR

  parse_args "$@"

  CONTAINER_INSTALL=0
  if (( USE_TOOLBOX && ! INSIDE_TOOLBOX )); then
    CONTAINER_INSTALL=1
  fi

  if (( INSIDE_TOOLBOX )); then
    info "Running inside Toolbox '$TOOLBOX_NAME'."
  fi

  detect_distribution
  if (( CONTAINER_INSTALL )); then
    check_toolbox_support
  fi
  preflight

  resolve_locale
  confirm_install
  select_locale
  resolve_region
  confirm_arch_upgrade
  init_log

  if (( CONTAINER_INSTALL )); then
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
    run 'Installing build dependencies' install_packages_tee
    VERBOSE=$prev_verbose
  else
    run 'Installing build dependencies' install_packages
  fi

  # Fail fast: surface an active Riot/League session before the long build,
  # but after dependency installation so hosts without Python can reach it.
  ensure_no_active_session

  command -v python3 >/dev/null 2>&1 || fail 'Python 3 is required.'
  run 'Checking FFmpeg compatibility' verify_ffmpeg
  run 'Running project preflight checks' python3 "$TARGET_DIR/scripts/preflight.py"
  run 'Preparing Python environment' prepare_python_env
  run 'Configuring Darling-LoL' configure_project
  run 'Building Darling-LoL (this may take a while)' compile_project
  ensure_no_active_session
  run 'Installing Darling system components' install_system_components
  run 'Installing the user runtime' install_user_runtime

  # Record the install mode only now that every step above succeeded: a
  # cancelled confirmation or a failed preflight/build must leave any previous
  # markers intact so scripts/update.sh keeps pointing at the last working
  # install. The Toolbox path execs into the container, so a CONTAINER_INSTALL
  # run never gets here; what does is the inner Toolbox run (DARLING_LOL_TOOLBOX=1,
  # with the container name passed by the outer installer) or a genuinely native
  # install. A manual run inside a container (INSIDE_TOOLBOX without
  # DARLING_LOL_TOOLBOX) records nothing: the container name is not known for sure.
  if [[ ${DARLING_LOL_TOOLBOX:-0} == 1 && $INSIDE_TOOLBOX == 1 ]]; then
    # Record the container so scripts/update.sh can re-exec itself inside it:
    # the build cache and the installed runtime belong to this container.
    mkdir -p "$TARGET_DIR/build"
    printf '%s\n' "${DARLING_LOL_TOOLBOX_NAME:-$TOOLBOX_NAME}" > "$TARGET_DIR/build/toolbox-container"
    rm -f "$TARGET_DIR/build/native-install"
  elif [[ $INSIDE_TOOLBOX != 1 ]]; then
    # Explicitly native install: record it so scripts/update.sh never re-execs
    # into a container, and drop a stale Toolbox marker.
    mkdir -p "$TARGET_DIR/build"
    : > "$TARGET_DIR/build/native-install"
    rm -f "$TARGET_DIR/build/toolbox-container"
  fi

  finish
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
