#!/bin/sh
# Bring an existing build and installation up to date after `git pull`: re-apply the patches that changed
# (prepare.py reverts the earlier version it finds applied), rebuild incrementally, and reinstall the base
# runtime, and the per-user overrides. Run it after manually closing Riot/League.
# Without arguments background steps use a progress spinner and an update log;
# --verbose streams that output to the terminal instead.
# Rootless callers must remain in their original namespace; the outer launcher
# refuses a live prefix marker instead of trying to reconnect or stop a session.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"

VERBOSE=0
BG_PID=''
LOG=''

usage() {
	printf '%s\n' \
		'Usage: sh scripts/update.sh [--verbose]' \
		'' \
		'Rebuild and reinstall Darling-LoL after `git pull`.' \
		'' \
		'  -v, --verbose  Show the full output of every build and install step.' \
		'  -h, --help     Show this help.' \
		'' \
		'Without --verbose background steps use a progress spinner and a log.' \
		'The privileged install keeps the terminal so sudo can authenticate.'
}

while [ "$#" -gt 0 ]; do
	case "$1" in
	-v | --verbose) VERBOSE=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		printf 'Unknown option: %s\n' "$1" >&2
		usage >&2
		exit 2
		;;
	esac
	shift
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
	esc=$(printf '\033')
	BOLD=$esc'[1m'
	DIM=$esc'[2m'
	RED=$esc'[31m'
	GREEN=$esc'[32m'
	YELLOW=$esc'[33m'
	CYAN=$esc'[36m'
	RESET=$esc'[0m'
else
	BOLD='' DIM='' RED='' GREEN='' YELLOW='' CYAN='' RESET=''
fi

case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
*[Uu][Tt][Ff]*8*)
	ICON_INFO='▶' ICON_OK='✔' ICON_ERR='✖' ICON_WARN='!'
	SPIN0='⠋' SPIN1='⠙' SPIN2='⠹' SPIN3='⠸' SPIN4='⠼'
	SPIN5='⠴' SPIN6='⠦' SPIN7='⠧' SPIN8='⠇' SPIN9='⠏'
	;;
*)
	ICON_INFO='>' ICON_OK='+' ICON_ERR='x' ICON_WARN='!'
	SPIN0='|' SPIN1='/' SPIN2='-' SPIN3='\' SPIN4='|'
	SPIN5='/' SPIN6='-' SPIN7='\' SPIN8='|' SPIN9='/'
	;;
esac

info() {
	printf '%s%s%s %s\n' "$CYAN" "$ICON_INFO" "$RESET" "$*"
}

success() {
	printf '%s%s%s %s\n' "$GREEN" "$ICON_OK" "$RESET" "$*"
}

warning() {
	printf '%s%s%s %s\n' "$YELLOW" "$ICON_WARN" "$RESET" "$*" >&2
}

error() {
	printf '%s%s ERROR:%s %s\n' "$RED$BOLD" "$ICON_ERR" "$RESET" "$*" >&2
}

now_seconds() {
	value=$(date +%s 2>/dev/null) || value=''
	case "$value" in
	'' | *[!0-9]*) printf 0 ;;
	*) printf '%s' "$value" ;;
	esac
}

# The spinner frame is chosen with a case so this stays POSIX sh (no arrays).
spin_line() {
	case $(($1 % 10)) in
	0) char=$SPIN0 ;;
	1) char=$SPIN1 ;;
	2) char=$SPIN2 ;;
	3) char=$SPIN3 ;;
	4) char=$SPIN4 ;;
	5) char=$SPIN5 ;;
	6) char=$SPIN6 ;;
	7) char=$SPIN7 ;;
	8) char=$SPIN8 ;;
	9) char=$SPIN9 ;;
	esac
	printf '\r%s%s%s %s' "$CYAN" "$char" "$RESET" "$2"
}

# The running step's process-group ID, but only when it leads its own group, so
# cleanup never signals the caller's group through a bogus negative PID.
step_group() {
	pgid=$(ps -o pgid= -p "$1" 2>/dev/null | tr -d ' ')
	case "$pgid" in
	'' | *[!0-9]*) return 1 ;;
	esac
	[ "$pgid" = "$1" ] || return 1
	printf '%s' "$pgid"
}

cleanup() {
	if [ -n "$BG_PID" ]; then
		# Stop the compilers and installers the step started too, not just its
		# direct child, then reap the child so it does not linger as a zombie.
		if group=$(step_group "$BG_PID"); then
			kill -TERM -"$group" 2>/dev/null || true
		else
			kill -TERM "$BG_PID" 2>/dev/null || true
		fi
		wait "$BG_PID" 2>/dev/null || true
		BG_PID=''
	fi
	if [ -t 1 ]; then
		printf '\033[?25h'
	fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Created lazily: --verbose runs stream to the terminal and need no file.
init_log() {
	log_dir=${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}
	if mkdir -p -- "$log_dir" 2>/dev/null; then
		LOG=$(mktemp -- "$log_dir/darling-lol-update.XXXXXXXX.log" 2>/dev/null) || LOG=''
	fi
}

show_log() {
	if [ -n "$LOG" ]; then
		printf '\n%sLog: %s%s\n' "$DIM" "$LOG" "$RESET"
	fi
}

# Run one update step. Privileged steps and steps without a log keep the terminal.
run() {
	description=$1
	shift
	rc=0
	spin=0
	start=$(now_seconds)

	if [ -n "$LOG" ] && [ "$1" != sudo ]; then
		printf '\n=== %s | %s ===\n' "$(date -Is 2>/dev/null || printf 'unknown time')" "$description" \
			2>/dev/null >>"$LOG" || LOG=''
	fi
	if [ "$VERBOSE" = 1 ] || [ "$1" = sudo ] || [ -z "$LOG" ]; then
		printf '\n%s%s %s%s\n' "$BOLD$CYAN" "$ICON_INFO" "$description" "$RESET"
		"$@" || rc=$?
	else
		if [ -t 1 ]; then
			spin=1
		else
			info "$description"
		fi
		# Keep the step in its own process group (and session) when setsid(1) is
		# available so cancellation can stop everything it started.
		if command -v setsid >/dev/null 2>&1; then
			setsid "$@" >>"$LOG" 2>&1 &
		else
			"$@" >>"$LOG" 2>&1 &
		fi
		BG_PID=$!

		if [ "$spin" = 1 ]; then
			printf '\033[?25l'
			frame=0
			while kill -0 "$BG_PID" 2>/dev/null; do
				spin_line "$frame" "$description"
				frame=$((frame + 1))
				sleep 0.12
			done
		fi

		wait "$BG_PID" || rc=$?
		BG_PID=''

		if [ "$spin" = 1 ]; then
			printf '\r\033[K\033[?25h'
		fi
	fi

	if [ "$rc" -ne 0 ]; then
		printf '%s%s%s %s\n' "$RED" "$ICON_ERR" "$RESET" "$description" >&2
		if [ -n "$LOG" ] && [ "$1" != sudo ]; then
			printf '\n%sLast log lines:%s\n' "$BOLD" "$RESET" >&2
			tail -n 25 "$LOG" >&2 || true
			printf '\nFull log: %s\n' "$LOG" >&2
		elif [ "$VERBOSE" = 0 ] && [ -z "$LOG" ]; then
			warning 'No update log was saved.'
		fi
		exit "$rc"
	fi

	end=$(now_seconds)
	elapsed=$((end - start))
	if [ "$elapsed" -lt 0 ]; then
		elapsed=0
	fi
	printf '%s%s%s %s %s(%dm%02ds)%s\n' \
		"$GREEN" "$ICON_OK" "$RESET" "$description" "$DIM" \
		"$((elapsed / 60))" "$((elapsed % 60))" "$RESET"
}

prepare_output() {
	# A background job must not already lead its own group when setsid runs.
	set +m
	if [ "$VERBOSE" = 0 ]; then
		init_log
	fi
}

# Older setups shipped one serial and UUID shared by every install. Give a prefix that still
# has them its own identity; any other saved values, and prefixes without them, are left alone.
replace_shared_identity() {
	python3 build/runtime/tools/stop-darling-runtime.py
	python3 tools/configure-darling-platform.py --replace-known-defaults --write ||
		warning 'Could not replace the shared platform identity; run tools/configure-darling-platform.py --provision --write'
}

if [ -n "${DARLING_WORKSPACE:-}" ]; then
	python3 scripts/nix-idle.py
	# Initial setup and upgrades use the same supported user-owned install path.
	# Keep each package closure rooted: copied ELF files can retain its loader.
	package=${DARLING_PACKAGE_ID%/share/darling-lol}
	case "$package" in
	/nix/store/*) ;;
	*)
		error 'Missing Nix package identity'
		exit 1
		;;
	esac
	mkdir -p build/nix-gcroots
	prepare_output
	printf '\n%s%sUpdating Darling-LoL%s\n' "$BOLD" "$CYAN" "$RESET"
	run 'Rooting the Nix package closure' \
		nix-store --realise "$package" --add-root "$repo/build/nix-gcroots/$(basename "$package")" --indirect
	run 'Marking the base runtime stale' python3 scripts/nix-state.py invalidate-base
	run 'Building Darling-LoL (this may take a while)' ./scripts/build.sh
	run 'Stopping the running runtime' python3 build/runtime/tools/stop-darling-runtime.py
	run 'Installing Darling system components' cmake --install build/darling-src/build
	run 'Recording the installed base runtime' python3 scripts/nix-state.py stamp-base
	run 'Installing the user runtime' ./scripts/install-runtime.sh
	replace_shared_identity
	show_log
	success 'Rootless update installed. In this shell use scripts/run.sh; otherwise exit this shell and start darling-lol.'
	exit 0
fi

# A Toolbox install builds against the container's libraries and installs the base
# runtime into the container's /usr/local, so the update must run in there too.
# Re-exec when running on the host; the container name comes from the marker
# written by install-native.sh, or from the installed desktop entry, or — only
# for legacy checkouts that left no record at all — from finding Darling in the
# installer's default container. A native install records build/native-install
# and is never redirected. DARLING_LOL_UPDATE_REEXEC stops the re-exec from
# looping when the inner run is not recognized as being inside the container.
if [ ! -f /run/.toolboxenv ] && [ -z "${DARLING_LOL_UPDATE_REEXEC:-}" ] &&
	[ ! -f build/native-install ]; then
	name=''
	if [ -f build/toolbox-container ]; then
		name=$(cat build/toolbox-container)
	else
		# $HOME holds a single launcher entry shared by every checkout: only trust
		# its container name when its Exec points at this checkout's scripts.
		# Reserved path characters come out escaped in the Exec value, so those
		# paths may not match here; a false negative is safe.
		entry="${HOME:-}/.local/share/applications/darling-launcher.desktop"
		name=$(grep '^Exec=' "$entry" 2>/dev/null | grep -F -- "$repo/scripts/" |
			sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' | head -n 1)
	fi
	# No record of any kind: only a legacy checkout can be assumed to live in the
	# installer's default container. This probe also starts a stopped container,
	# so it must not decide for checkouts that are already recorded above.
	if [ -z "$name" ] && command -v toolbox >/dev/null 2>&1 &&
		toolbox run -c darling-lol sh -c 'command -v darling' >/dev/null 2>&1; then
		name=darling-lol
	fi
	if [ -n "$name" ]; then
		if ! command -v toolbox >/dev/null 2>&1; then
			error "This checkout was installed via Toolbox container '$name', but Toolbox"
			printf '  is not installed on this host. Install Toolbox, then run the update\n' >&2
			printf '  inside the container: toolbox enter %s\n' "$name" >&2
			exit 1
		fi
		if ! toolbox run -c "$name" true 2>/dev/null; then
			error "This checkout was installed via Toolbox container '$name', but that container is not"
			printf '  reachable. Start it and run the update inside it: toolbox enter %s\n' "$name" >&2
			exit 1
		fi
		info "Toolbox install detected; running the update inside container '$name'..."
		if [ "$VERBOSE" = 1 ]; then
			exec toolbox run --container "$name" env DARLING_LOL_UPDATE_REEXEC=1 \
				sh "$repo/scripts/update.sh" --verbose
		fi
		exec toolbox run --container "$name" env DARLING_LOL_UPDATE_REEXEC=1 sh "$repo/scripts/update.sh"
	fi
fi

# The re-exec lands here inside the container: say so, and name it when the
# install marker that selected it is still in the checkout.
if [ -f /run/.toolboxenv ]; then
	name=''
	if [ -f build/toolbox-container ]; then
		IFS= read -r name <build/toolbox-container || true
	fi
	if [ -n "$name" ]; then
		info "Running inside Toolbox container '$name'."
	else
		info 'Running inside a Toolbox container.'
	fi
fi

if [ ! -d build/darling-src/build ]; then
	error 'No existing build; follow the first-time setup in README.md instead.'
	exit 1
fi
# The install steps shut Darling down and stop leftover Riot processes, so refuse rather than end a session.
python3 - <<'PY'
from pathlib import Path
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        command_line = (entry / 'cmdline').read_bytes()
    except OSError:
        continue
    if b'/Riot Client.app/' in command_line or b'/League of Legends.app/' in command_line:
        raise SystemExit('A Riot Client or League process is running; exit the game and both clients, then run this again.')
PY

if ! command -v sudo >/dev/null 2>&1; then
	error 'sudo is required to install the system components.'
	exit 1
fi
prepare_output
printf '\n%s%sUpdating Darling-LoL%s\n' "$BOLD" "$CYAN" "$RESET"

run 'Building Darling-LoL (this may take a while)' ./scripts/build.sh
run 'Stopping the running runtime' python3 build/runtime/tools/stop-darling-runtime.py
run 'Installing Darling system components' sudo cmake --install build/darling-src/build
run 'Installing the user runtime' ./scripts/install-runtime.sh
replace_shared_identity
show_log
success 'Update installed. Start Darling with: sh scripts/run.sh'
