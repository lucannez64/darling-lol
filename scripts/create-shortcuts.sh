#!/usr/bin/env bash
#
# Create, update or remove the Darling-LoL desktop shortcuts interactively:
# the launcher shortcut with a custom name, the hidden window entries that
# name and icon the League client, game and Riot Client windows, the launch
# region and language derived from the system locale, custom launch flags
# (environment variables) baked into the Exec line, and whether the shortcut
# runs on the host or re-enters a Toolbox container. The work itself is done
# by build/runtime/tools/desktop-entry.py, the same helper the non-interactive
# scripts/create-desktop-entry.sh and the installer use.

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
TARGET_DIR=$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)
HELPER="$TARGET_DIR/build/runtime/tools/desktop-entry.py"
LAUNCHER_ENTRY="${HOME:-}/.local/share/applications/darling-launcher.desktop"

readonly DEFAULT_NAME='Darling Launcher'
readonly DEFAULT_TOOLBOX='darling-lol'
# Locales the Riot Client ships; keep in sync with LOCALES in
# scripts/install-native.sh and scripts/common.py (tests/test_create_shortcuts.py
# compares the list with the one configure.py uses).
readonly -a LOCALES=(
  ar_AE cs_CZ de_DE el_GR en_AU en_GB en_PH en_SG en_US es_AR es_ES es_MX
  fr_FR hu_HU id_ID it_IT ja_JP ko_KR pl_PL pt_BR ro_RO ru_RU th_TH tr_TR
  vi_VN zh_CN zh_MY zh_TW
)

ACTION=''
YES=0
NAME=''
TOOLBOX=''
TOOLBOX_SET=0
# 1 when the hidden League/Riot Client window entries were requested
# (--window-entries or DARLING_LOL_WINDOW_ENTRIES=1); never created by default.
WINDOW_ENTRIES=0
if [[ ${DARLING_LOL_WINDOW_ENTRIES:-0} == 1 ]]; then WINDOW_ENTRIES=1; fi
ENV_FLAGS=()
# 1 when --env supplied the launch flags: they replace the automatic
# RCLOL_REGION/RCLOL_LOCALE pair derived from the system locale.
ENV_FLAGS_EXPLICIT=0
REMOVE_HANDLER=0
ICON_CUSTOM=0
# Resolved custom icon (empty = the helper's default project icon).
ICON_ARG=''
INSIDE_TOOLBOX=0
# Settings of the installed launcher entry, used as update-mode defaults.
CURRENT_OWNED=0
CURRENT_NAME=''
CURRENT_ICON=''
CURRENT_ENV=()

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
else
  ICON_INFO='>' ICON_OK='+' ICON_ERR='x' ICON_WARN='!'
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

interactive_run() {
  (( ! YES )) && [[ -t 0 ]]
}

usage() {
  cat <<EOF
Usage: ${0##*/} [options] [action]

Create, update or remove the Darling-LoL desktop shortcuts interactively.

Actions (chosen from the menu when not given):
  --create           Create new shortcuts
  --update           Recreate the installed shortcuts, offering their current
                     settings as the defaults for the questions
  --remove           Remove the shortcuts this project created

Options:
  -y, --yes          Accept the defaults without prompting (required without a
                     terminal; an action is required too)
  --name NAME        Launcher shortcut name (default: Darling Launcher)
  --toolbox NAME     Run the shortcut through the given Toolbox container;
                     an empty value runs it on the host
  --window-entries   Also create the hidden entries that name and icon the
                     League client, game and Riot Client windows in the GNOME
                     overview (GNOME only; the question is skipped elsewhere)
  --env VAR=VALUE    Launch flag written into the Exec line; repeatable.
                     When given, the flags replace the automatic
                     RCLOL_REGION/RCLOL_LOCALE pair offered from the
                     system locale
  --icon             Use a custom-icon.* file (svg, png, ...) from the project
                     root as the shortcut icon instead of the project icon
  --url-handler      With --remove, also remove the riotclient:// sign-in
                     link handler
  -h, --help         Show this help

Environment:
  DARLING_LOL_WINDOW_ENTRIES   Same as --window-entries when set to 1
  DARLING_LOL_TOOLBOX_NAME     Toolbox container name when running inside one
  NO_COLOR                     Disable colored output
EOF
}

add_env_flag() {
  local value=$1 source=$2
  if [[ ! $value =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
    fail "$source expects NAME=VALUE, got '$value'."
  fi
  ENV_FLAGS+=("$value")
  ENV_FLAGS_EXPLICIT=1
}

parse_args() {
  while (( $# )); do
    case $1 in
      -y|--yes) YES=1 ;;
      --create|--update|--remove)
        local requested=${1#--}
        if [[ -n $ACTION && $ACTION != "$requested" ]]; then
          usage >&2
          fail "Only one action may be given (--$ACTION and $1 are both present)."
        fi
        ACTION=$requested
        ;;
      --name)
        (( $# >= 2 )) || fail '--name requires a value.'
        [[ -n $2 ]] || fail '--name requires a non-empty value.'
        NAME=$2
        shift
        ;;
      --name=*)
        [[ -n ${1#--name=} ]] || fail '--name requires a non-empty value.'
        NAME=${1#--name=}
        ;;
      --toolbox)
        (( $# >= 2 )) || fail '--toolbox requires a value.'
        TOOLBOX=$2; TOOLBOX_SET=1
        shift
        ;;
      --toolbox=*) TOOLBOX=${1#--toolbox=}; TOOLBOX_SET=1 ;;
      --window-entries) WINDOW_ENTRIES=1 ;;
      --env)
        (( $# >= 2 )) || fail '--env requires a NAME=VALUE value.'
        add_env_flag "$2" '--env'
        shift
        ;;
      --env=*) add_env_flag "${1#--env=}" '--env' ;;
      --icon) ICON_CUSTOM=1 ;;
      --url-handler) REMOVE_HANDLER=1 ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; fail "Unknown option: $1" ;;
    esac
    shift
  done
}

toolbox_available() {
  command -v toolbox >/dev/null 2>&1 || return 1
  toolbox run -c "$1" true >/dev/null 2>&1
}

# A Toolbox installation is detected the same way scripts/update.sh does it:
# the marker recorded by the installer, then the container named in an
# installed shortcut's Exec line, then the installer's default container when
# it actually provides Darling.
detect_toolbox_install() {
  local name=''
  if [[ -f $TARGET_DIR/build/toolbox-container ]]; then
    name=$(<"$TARGET_DIR/build/toolbox-container")
  fi
  if [[ -z $name && -f $LAUNCHER_ENTRY ]]; then
    name=$(sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' "$LAUNCHER_ENTRY" 2>/dev/null | head -n 1)
    name=${name%\"}
    name=${name#\"}
  fi
  if [[ -z $name ]] && command -v toolbox >/dev/null 2>&1 &&
     toolbox run -c "$DEFAULT_TOOLBOX" sh -c 'command -v darling' >/dev/null 2>&1; then
    name=$DEFAULT_TOOLBOX
  fi
  [[ -n $name ]] || return 1
  # A stale marker or an outdated entry must not suggest a dead container.
  if (( ! INSIDE_TOOLBOX )) && ! toolbox_available "$name"; then
    warning "Toolbox container '$name' was detected but is not usable; ignoring it."
    return 1
  fi
  printf '%s' "$name"
}

# Where the shortcut runs. Inside Toolbox there is no choice: the host has no
# Darling, so the entry must re-enter the container. Otherwise a detected
# Toolbox installation is suggested as the default; without one, the host is
# the default and a container is opt-in.
choose_target() {
  if (( INSIDE_TOOLBOX )); then
    local container=${DARLING_LOL_TOOLBOX_NAME:-}
    if [[ -z $container && -f $TARGET_DIR/build/toolbox-container ]]; then
      container=$(<"$TARGET_DIR/build/toolbox-container")
    fi
    container=${container:-$DEFAULT_TOOLBOX}
    if (( TOOLBOX_SET )) && [[ $TOOLBOX != "$container" ]]; then
      warning "Running inside Toolbox '$container'; ignoring --toolbox '$TOOLBOX'."
    fi
    TOOLBOX=$container
    info "Running inside Toolbox '$TOOLBOX': the shortcut will re-enter this container."
    return 0
  fi

  local detected=''
  detected=$(detect_toolbox_install || true)

  if (( TOOLBOX_SET )); then
    if [[ -n $TOOLBOX ]] && ! toolbox_available "$TOOLBOX"; then
      fail "Toolbox container '$TOOLBOX' is not usable. Start it with 'toolbox enter $TOOLBOX'."
    fi
    return 0
  fi

  if [[ -n $detected ]]; then
    if interactive_run; then
      local reply
      while true; do
        read -r -p "${CYAN}${ICON_INFO}${RESET} Run the shortcut through Toolbox '$detected'? [Y/n]: " reply ||
          fail 'No input received while choosing where the shortcut runs.'
        case ${reply,,} in
          ''|y|yes) TOOLBOX=$detected; break ;;
          n|no) TOOLBOX=''; break ;;
          *) warning 'Invalid answer. Enter y or n.' ;;
        esac
      done
    else
      TOOLBOX=$detected
    fi
    if [[ -n $TOOLBOX ]]; then
      info "Shortcut target: Toolbox '$TOOLBOX' (detected installation)."
    else
      info 'Shortcut target: the host.'
    fi
    return 0
  fi

  TOOLBOX=''
  if ! interactive_run; then
    info 'Shortcut target: the host (no Toolbox installation detected).'
    return 0
  fi
  local reply
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Run the shortcut through a Toolbox container? [y/N]: " reply ||
      fail 'No input received while choosing where the shortcut runs.'
    case ${reply,,} in
      ''|n|no) info 'Shortcut target: the host.'; return 0 ;;
      y|yes) break ;;
      *) warning 'Invalid answer. Enter y or n.' ;;
    esac
  done
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Toolbox container name [$DEFAULT_TOOLBOX]: " reply ||
      fail 'No input received while naming the container.'
    reply=${reply:-$DEFAULT_TOOLBOX}
    if toolbox_available "$reply"; then
      TOOLBOX=$reply
      info "Shortcut target: Toolbox '$TOOLBOX'."
      return 0
    fi
    warning "Toolbox container '$reply' is not usable. Start it with 'toolbox enter $reply' and try again."
  done
}

# The display name of the launcher shortcut; Enter keeps the default (or, in
# update mode, the name the installed entry already has).
choose_name() {
  local default=$1 reply
  if [[ -n $NAME ]]; then
    return 0
  fi
  if ! interactive_run; then
    NAME=$default
    return 0
  fi
  read -r -p "${CYAN}${ICON_INFO}${RESET} Launcher shortcut name [${default}]: " reply ||
    fail 'No input received while naming the shortcut.'
  reply=${reply#"${reply%%[![:space:]]*}"}
  reply=${reply%"${reply##*[![:space:]]}"}
  NAME=${reply:-$default}
}

# The hidden window entries only help GNOME: gnome-shell matches client
# windows to desktop entries by StartupWMClass to name and icon them in the
# overview and the dash. Other sessions cannot use them, so the question is
# skipped there and no window entries are created (and ones this project
# wrote before are cleaned up, like --window-entries being dropped does).
is_gnome_session() {
  case ${XDG_CURRENT_DESKTOP:-} in
    *GNOME*|*gnome*) return 0 ;;
    *) return 1 ;;
  esac
}

choose_window_entries() {
  local reply
  if (( WINDOW_ENTRIES )); then
    return 0
  fi
  if ! is_gnome_session; then
    WINDOW_ENTRIES=0
    if [[ -f ${HOME:-}/.local/share/applications/darling-league-client.desktop ]] ||
       [[ -f ${HOME:-}/.local/share/applications/darling-riot-client.desktop ]]; then
      info 'Window entries only help GNOME and the current session is not GNOME; they will be removed.'
    else
      info 'Window entries only help GNOME and the current session is not GNOME.'
    fi
    return 0
  fi
  # A GNOME session names and icons the client windows through the entries,
  # so that is the default answer.
  if ! interactive_run; then
    WINDOW_ENTRIES=1
    return 0
  fi
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Name and icon the League client, game and Riot Client windows in the GNOME overview? [Y/n]: " reply ||
      fail 'No input received while choosing the window entries.'
    case ${reply,,} in
      ''|y|yes) WINDOW_ENTRIES=1; return 0 ;;
      n|no) WINDOW_ENTRIES=0; return 0 ;;
      *) warning 'Invalid answer. Enter y or n.' ;;
    esac
  done
}

# Region and language detection copied from scripts/install-native.sh so the
# shortcut can bake the desktop session's own server and client language into
# its Exec line.
normalize_locale() {
  # Case conversion and the ASCII ranges must not follow a locale such as
  # tr_TR, where they would produce non-ASCII codes.
  local LC_ALL=C value=${1//-/_}
  if [[ $value =~ ^([A-Za-z]{2})_([A-Za-z]{2})$ ]]; then
    printf '%s_%s' "${BASH_REMATCH[1],,}" "${BASH_REMATCH[2]^^}"
    return 0
  fi
  return 1
}

valid_locale() {
  local entry
  for entry in "${LOCALES[@]}"; do
    [[ $entry == "$1" ]] && return 0
  done
  return 1
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

# The system locale when the Riot Client ships it; fails otherwise (fr_CA,
# C, ...), so the caller can keep the shortcut neutral and say why.
supported_system_locale() {
  local detected
  detected=$(detect_system_locale) || return 1
  valid_locale "$detected" || return 1
  printf '%s' "$detected"
}

region_for_country() {
  # Map a locale's country part to a Riot region; unknown countries fail, so
  # callers can tell a real match apart from the generic default.
  local country=${1:-}

  case "$country" in
    DE|ES|FR|GB|IT) printf 'EUW' ;;
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
  region_for_country "${1#*_}"
}

# The darlingserver affinity switch only helps a hybrid Intel CPU whose
# performance cores the kernel lists in sysfs (the same file the server reads);
# anywhere else the server logs that it has nothing to pin and runs unchanged.
# DARLING_LOL_PCORE_CPUS is a test seam, like DARLING_OS_RELEASE in the installer.
pcore_affinity_helps() {
  local file=${DARLING_LOL_PCORE_CPUS:-/sys/devices/cpu_core/cpus} cpus=''
  [[ -r $file ]] || return 1
  read -r cpus < "$file" || return 1
  [[ -n $cpus ]]
}

show_flag_reference() {
  local pcore_recommended=''
  if pcore_affinity_helps; then
    pcore_recommended=' (recommended)'
  fi
  printf '\n%sDocumented launch flags (see docs/SETUP.md and docs/TROUBLESHOOTING.md):%s\n' \
    "$BOLD" "$RESET"
  printf '  RCLOL_HIDE_LEAGUE_SPLASH=1        Hide the splash window that can appear over the client\n'
  printf '  RCLOL_CLIENT_IN_GAME_TRICKS=1     Close or hide the client automatically during a match\n'
  printf '  RCLOL_CLOSE_CLIENT_IN_GAME=1      Close the League client during a match (low-level)\n'
  printf '  RCLOL_HIDE_CLIENT_IN_GAME=1       Hide the League client during a match (low-level)\n'
  printf '  RCLOL_HIDE_RIOT_CLIENT_IN_GAME=1  Hide the Riot Client during a match\n'
  printf '  RCLOL_CDN_IPV4_ONLY=0             Disable the CDN IPv4-only resolver workaround\n'
  printf '  RCLOL_RECOVER_STALE_CLIENT=1      Recover a stale windowless client at launch\n'
  printf '  RCLOL_RECOVER_HIDDEN_CLIENT=1     Recover a hidden client at launch\n'
  printf '  RCLOL_CLEAN_STALE_HELPERS=1       Remove orphaned in-game helpers at launch\n'
  printf '  RCLOL_CLEAN_ON_EXIT=0             Keep the runtime running after the clients exit\n'
  printf '  RCLOL_STOP_AFTER_CLIENT_SECONDS=N  Stop the runtime N seconds after the last window closes (default: 60)\n'
  printf '  RCLOL_STOP_TOOLBOX=0              Keep the Toolbox container running after the session\n'
  printf '  RCLOL_DISCORD_RPC=0               Disable the Discord Rich Presence links\n'
  printf '  RCLOL_FILTER_HOST_ENV=1           Remove host desktop-session variables at launch\n'
  printf '  RCLOL_FINISH_LEAGUE_LOAD=0        Disable the League load finish workaround\n'
  printf '  RCLOL_PLATFORM_PROFILE=host       Override the platform identity profile (host|compat)\n'
  printf '  RCLOL_REGION=CODE                 Server region for the launch, for example BR\n'
  printf '  RCLOL_LOCALE=CODE                 Client language for the launch, for example pt_BR\n'
  printf '  DSERVER_PCORE_AFFINITY=1          Keep the server threads on the performance cores of a hybrid Intel CPU%s\n' \
    "$pcore_recommended"
  printf '\n'
}

# Custom launch flags are environment variables written into the Exec line.
# The question comes first; the documented list below is only a reference,
# shown after a yes, and the answer itself is free-form NAME=VALUE input.
choose_flags() {
  local reply token invalid accepted=() reply_default='' question
  if (( ${#ENV_FLAGS[@]} )); then
    return 0
  fi
  # An update starts from the installed flags, so accepting every default
  # keeps them; 'none' at the value prompt is the explicit way to clear them.
  if (( CURRENT_OWNED )) && (( ${#CURRENT_ENV[@]} )); then
    ENV_FLAGS=("${CURRENT_ENV[@]}")
    reply_default=${ENV_FLAGS[*]}
    question='Change the launch flags (environment variables)?'
  else
    question='Add custom launch flags (environment variables)?'
  fi
  if ! interactive_run; then
    return 0
  fi
  if [[ -n $reply_default ]]; then
    printf '%sCurrent flags: %s%s\n' "$DIM" "$reply_default" "$RESET"
  fi
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} ${question} [y/N]: " reply ||
      fail 'No input received while choosing the launch flags.'
    case ${reply,,} in
      ''|n|no) return 0 ;;
      y|yes) break ;;
      *) warning 'Invalid answer. Enter y or n.' ;;
    esac
  done
  show_flag_reference
  if [[ -n $reply_default ]]; then
    info "Enter keeps the current flags; type 'none' to clear them."
  fi
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Launch flags (NAME=VALUE, space-separated) [${reply_default:-none}]: " reply ||
      fail 'No input received while entering the launch flags.'
    # Enter keeps the current array exactly: an installed value with spaces
    # cannot be retyped in this space-separated format.
    if [[ -z $reply ]]; then
      return 0
    fi
    if [[ ${reply,,} == none ]]; then
      ENV_FLAGS=()
      return 0
    fi
    invalid=''
    accepted=()
    for token in $reply; do
      if [[ $token =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
        accepted+=("$token")
      else
        invalid+=" $token"
      fi
    done
    if [[ -z $invalid ]]; then
      ENV_FLAGS=("${accepted[@]}")
      return 0
    fi
    warning "Invalid launch flag(s):${invalid}. Expected NAME=VALUE."
  done
}

# Offer the region and language the desktop session already uses. The answer
# defaults to yes (like the installer's locale suggestion), and unattended runs
# take the default. Explicit flags win: --env, or an installed
# RCLOL_REGION/RCLOL_LOCALE that an update keeps, skip the question.
choose_region_locale() {
  local flag locale region reply prompt
  if (( ENV_FLAGS_EXPLICIT )); then
    return 0
  fi
  for flag in "${ENV_FLAGS[@]}"; do
    case $flag in
      RCLOL_REGION=*|RCLOL_LOCALE=*) return 0 ;;
    esac
  done
  if ! locale=$(supported_system_locale); then
    info 'The system locale is not one the Riot Client ships; not adding RCLOL_REGION/RCLOL_LOCALE.'
    return 0
  fi
  region=$(region_for_locale "$locale") || region=''
  if [[ -n $region ]]; then
    prompt="Use your system region and language (${region}, ${locale}) for the launch?"
  else
    prompt="Use your system language (${locale}) and keep the configured region for the launch?"
  fi
  if ! interactive_run; then
    # An unattended update keeps what the installed shortcut already has; only
    # creation (or an update that falls back to creation) takes the suggestion.
    if (( CURRENT_OWNED )); then
      return 0
    fi
    if [[ -n $region ]]; then
      info "Using the system region and language: $region / $locale."
    else
      info "Using the system language ($locale) and keeping the configured region."
    fi
  else
    while true; do
      read -r -p "${CYAN}${ICON_INFO}${RESET} $prompt [Y/n]: " reply ||
        fail 'No input received while choosing the region and language.'
      case ${reply,,} in
        ''|y|yes) break ;;
        n|no) return 0 ;;
        *) warning 'Invalid answer. Enter y or n.' ;;
      esac
    done
  fi
  if [[ -n $region ]]; then
    ENV_FLAGS+=("RCLOL_REGION=$region")
  fi
  ENV_FLAGS+=("RCLOL_LOCALE=$locale")
}

# Update-mode defaults parsed from the installed launcher entry (helper --show).
read_current() {
  local line output=''
  CURRENT_OWNED=0
  CURRENT_NAME=''
  CURRENT_ICON=''
  CURRENT_ENV=()
  if ! output=$(python3 "$HELPER" --show </dev/null 2>/dev/null); then
    return 0
  fi
  while IFS= read -r line; do
    case $line in
      owned=yes) CURRENT_OWNED=1 ;;
      name=*) CURRENT_NAME=${line#name=} ;;
      icon=*) CURRENT_ICON=${line#icon=} ;;
      env=*) CURRENT_ENV+=("${line#env=}") ;;
    esac
  done <<<"$output"
}

# Resolve which icon the shortcuts get. With --icon the user's custom-icon.*
# file in the project root wins (it is never versioned, see .gitignore). An
# update without --icon keeps an installed custom icon while its file still
# exists; when the file is gone the shortcuts fall back to the default icon.
resolve_icon() {
  ICON_ARG=''
  if (( ICON_CUSTOM )); then
    ICON_ARG=$(custom_icon_path)
    return 0
  fi
  if [[ $ACTION == update && $CURRENT_OWNED == 1 && -n $CURRENT_ICON &&
        $CURRENT_ICON != 'darling-launcher' ]]; then
    if [[ -f $CURRENT_ICON ]]; then
      ICON_ARG=$CURRENT_ICON
      info "Keeping the custom icon: $ICON_ARG"
    else
      warning "The installed shortcut uses the custom icon '$CURRENT_ICON', which no longer exists; falling back to the project icon."
    fi
  fi
}

custom_icon_path() {
  local file found=()
  for file in "$TARGET_DIR"/custom-icon.*; do
    [[ -f $file ]] && found+=("$file")
  done
  if (( ${#found[@]} == 0 )); then
    fail '--icon expects one custom-icon.* file (svg, png, ...) in the project root.'
  fi
  if (( ${#found[@]} > 1 )); then
    fail "Keep exactly one custom-icon.* file in the project root; found: ${found[*]}."
  fi
  printf '%s' "${found[0]}"
}

# Render the helper like scripts/create-desktop-entry.sh does; the render is
# generated and the run is cheap and idempotent.
ensure_helper() {
  command -v python3 >/dev/null 2>&1 || fail 'Python 3 is required.'
  ( cd "$TARGET_DIR" && python3 scripts/configure.py >/dev/null ) ||
    fail 'Could not render build/runtime/tools/desktop-entry.py; run python3 scripts/configure.py.'
  [[ -f $HELPER ]] || fail "The desktop-entry helper is missing at $HELPER."
}

show_banner() {
  printf '\n%s%sDarling-LoL shortcut manager%s\n' "$BOLD" "$CYAN" "$RESET"
  printf '%sHelper: %s%s\n' "$DIM" "$HELPER" "$RESET"
  if (( INSIDE_TOOLBOX )); then
    printf "%sMode: inside Toolbox '%s'%s\n" "$DIM" \
      "${DARLING_LOL_TOOLBOX_NAME:-$DEFAULT_TOOLBOX}" "$RESET"
  fi
}

show_menu() {
  if (( YES )) || [[ ! -t 0 ]]; then
    fail 'Choose an action with --create, --update or --remove when running without a terminal.'
  fi
  local reply
  while true; do
    printf '\n%s%sDarling-LoL shortcut manager%s\n\n' "$BOLD" "$CYAN" "$RESET"
    printf '  %s1)%s Create new shortcuts\n' "$CYAN" "$RESET"
    printf '  %s2)%s Update the installed shortcuts\n' "$CYAN" "$RESET"
    printf '  %s3)%s Remove the installed shortcuts\n' "$CYAN" "$RESET"
    printf '  %s0)%s Exit\n' "$CYAN" "$RESET"
    read -r -p "${CYAN}${ICON_INFO}${RESET} Choose an action [1-3, 0]: " reply ||
      fail 'No input received while choosing an action.'
    case $reply in
      1) ACTION=create; return 0 ;;
      2) ACTION=update; return 0 ;;
      3) ACTION=remove; return 0 ;;
      0) exit 0 ;;
      *) warning 'Invalid choice. Enter 1, 2, 3 or 0.' ;;
    esac
  done
}

summarize() {
  printf '\n%s%sShortcut summary%s\n' "$BOLD" "$CYAN" "$RESET"
  printf '%sName: %s%s\n' "$DIM" "$NAME" "$RESET"
  if [[ -n $TOOLBOX ]]; then
    printf "%sTarget: Toolbox '%s'%s\n" "$DIM" "$TOOLBOX" "$RESET"
  else
    printf '%sTarget: the host%s\n' "$DIM" "$RESET"
  fi
  if (( WINDOW_ENTRIES )); then
    printf '%sWindow entries: League of Legends (client and game), Riot Client%s\n' "$DIM" "$RESET"
  else
    printf '%sWindow entries: no%s\n' "$DIM" "$RESET"
  fi
  if (( ${#ENV_FLAGS[@]} )); then
    printf '%sLaunch flags: %s%s\n' "$DIM" "${ENV_FLAGS[*]}" "$RESET"
  else
    printf '%sLaunch flags: none%s\n' "$DIM" "$RESET"
  fi
  if [[ -n $ICON_ARG ]]; then
    printf '%sIcon: %s%s\n' "$DIM" "$ICON_ARG" "$RESET"
  else
    printf '%sIcon: the project icon%s\n' "$DIM" "$RESET"
  fi
  printf '%sMenu entry: %s%s\n' "$DIM" "$LAUNCHER_ENTRY" "$RESET"
}

summarize_remove() {
  printf '\n%s%sRemoval summary%s\n' "$BOLD" "$CYAN" "$RESET"
  printf '%sRemoves: the launcher shortcut, its desktop copies and the hidden window entries%s\n' \
    "$DIM" "$RESET"
  printf '%sKeeps: files this project did not create and the launcher icons%s\n' "$DIM" "$RESET"
  if (( REMOVE_HANDLER )); then
    printf '%sAlso removes: the riotclient:// sign-in link handler%s\n' "$DIM" "$RESET"
  fi
  printf '%sNote: social sign-in needs the link handler while the shortcuts are gone%s\n' \
    "$DIM" "$RESET"
}

confirm_action() {
  local prompt=$1 default_yes=$2 reply
  if (( YES )); then return 0; fi
  [[ -t 0 ]] || fail 'No interactive terminal available. Re-run with --yes.'
  read -r -p "${CYAN}${ICON_INFO}${RESET} ${prompt} " reply ||
    fail 'Cancelled.'
  case ${reply,,} in
    '') if (( default_yes )); then return 0; fi ;;
    y|yes) return 0 ;;
  esac
  fail 'Cancelled.'
}

run_helper() {
  local output='' rc=0 line
  output=$(python3 "$HELPER" "$@") || rc=$?
  if [[ -n $output ]]; then
    while IFS= read -r line; do
      printf '%s%s%s\n' "$DIM" "$line" "$RESET"
    done <<<"$output"
  fi
  if (( rc != 0 )); then
    fail "The desktop-entry helper failed with exit code $rc."
  fi
}

run_create_update() {
  local args=(--name "$NAME") flag
  if [[ -n $TOOLBOX ]]; then args+=(--toolbox "$TOOLBOX"); fi
  if (( WINDOW_ENTRIES )); then args+=(--window-entries); fi
  if (( ${#ENV_FLAGS[@]} )); then
    for flag in "${ENV_FLAGS[@]}"; do args+=(--env "$flag"); done
  fi
  if [[ -n $ICON_ARG ]]; then args+=(--icon "$ICON_ARG"); fi
  run_helper "${args[@]}"
  success "Shortcuts written for '$NAME'."
}

choose_removal_scope() {
  local reply
  if (( REMOVE_HANDLER )) || ! interactive_run; then return 0; fi
  while true; do
    read -r -p "${CYAN}${ICON_INFO}${RESET} Also remove the riotclient:// sign-in link handler? [y/N]: " reply ||
      fail 'No input received while choosing the removal scope.'
    case ${reply,,} in
      ''|n|no) return 0 ;;
      y|yes) REMOVE_HANDLER=1; return 0 ;;
      *) warning 'Invalid answer. Enter y or n.' ;;
    esac
  done
}

run_remove() {
  local args=(--remove)
  if (( REMOVE_HANDLER )); then args+=(--remove-all); fi
  run_helper "${args[@]}"
  success 'Removal finished.'
}

main() {
  set -Eeuo pipefail
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'printf "\n%s%s Unexpected failure at line %s: %s%s\n" "$RED" "$ICON_ERR" "$LINENO" "$BASH_COMMAND" "$RESET" >&2' ERR

  parse_args "$@"

  if [[ $ACTION == remove && $ICON_CUSTOM == 1 ]]; then
    fail '--icon is only valid when creating or updating shortcuts.'
  fi

  show_banner

  if [[ -z $ACTION ]]; then
    show_menu
  fi

  ensure_helper

  case $ACTION in
    remove)
      choose_removal_scope
      summarize_remove
      confirm_action 'Proceed with the removal? [y/N]' 0
      run_remove
      ;;
    create|update)
      local default_name=$DEFAULT_NAME
      if [[ $ACTION == update ]]; then
        read_current
        if (( CURRENT_OWNED )); then
          if [[ -n $CURRENT_NAME ]]; then default_name=$CURRENT_NAME; fi
        else
          info 'No installed launcher shortcut of this project was found; creating new shortcuts.'
        fi
      fi
      choose_name "$default_name"
      choose_window_entries
      choose_flags
      choose_region_locale
      choose_target
      resolve_icon
      summarize
      confirm_action "Write the shortcuts for '$NAME'? [Y/n]" 1
      run_create_update
      ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
