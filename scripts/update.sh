#!/bin/sh
# Bring an existing build and installation up to date after `git pull`: re-apply the patches that changed
# (prepare.py reverts the earlier version it finds applied), rebuild incrementally, and reinstall the base
# runtime, and the per-user overrides. Run it after manually closing Riot/League.
# A Toolbox install re-enters the container recorded at install time and stops it again when this run
# started or restarted it.
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
# Toolbox container this run started (or restarted); the EXIT trap stops it so
# a one-shot update does not leave a container running behind.
TOOLBOX_STOP=''

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
	if [ -n "$TOOLBOX_STOP" ]; then
		stop_toolbox_container "$TOOLBOX_STOP" || true
		TOOLBOX_STOP=''
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

# The base install copies about 33 000 files and re-evaluates every rule even
# when the build tree did not change; the stamp is recorded after a successful
# install and lets the next update skip both the copy and the sudo prompt.
install_system_components() {
	if python3 build/runtime/tools/darling-install-stamp.py --root "$repo" check; then
		success 'Darling system components are up to date'
		return 0
	fi
	run 'Installing Darling system components' "$@"
	python3 build/runtime/tools/darling-install-stamp.py --root "$repo" record || \
		warning 'Install stamp not recorded; the next update reinstalls the system components'
}

# Run one update step. Privileged steps and steps without a log keep the terminal.
run() {
	description=$1
	shift
	rc=0
	spin=0
	start=$(now_seconds)

	if [ -n "$LOG" ]; then
		printf '\n=== %s | %s ===\n' "$(date -Is 2>/dev/null || printf 'unknown time')" "$description" \
			2>/dev/null >>"$LOG" || LOG=''
	fi
	if [ "$VERBOSE" = 0 ] && [ "$1" = sudo ] && [ -n "$LOG" ]; then
		# Ask for the password on the terminal first, then log the quiet step.
		# The step must not use setsid: sudo's cached credentials are per tty.
		sudo -v || exit $?
		shift
		set -- sudo -n "$@"
		nosetsid=1
	else
		nosetsid=0
	fi
	if [ "$VERBOSE" = 1 ] || { [ "$1" = sudo ] && [ "$nosetsid" = 0 ]; } || [ -z "$LOG" ]; then
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
		if [ "$nosetsid" = 0 ] && command -v setsid >/dev/null 2>&1; then
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
		if [ -n "$LOG" ] && { [ "$1" != sudo ] || [ "$nosetsid" = 1 ]; }; then
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

# Older setups shipped one serial and UUID shared by every install, and prefixes set up before
# provisioning have no saved identity at all (iokitd then reports a DARLING- serial). Give such a
# prefix its own identity; custom saved values are left alone.
replace_shared_identity() {
	python3 build/runtime/tools/stop-darling-runtime.py
	python3 tools/configure-darling-platform.py --replace-known-defaults --write ||
		warning 'Could not replace the shared platform identity; run tools/configure-darling-platform.py --provision --write'
}

warn_if_runtime_unsigned() {
	if [ "$VERBOSE" = 0 ] && [ -n "$LOG" ] && [ -r "$LOG" ] &&
		grep -q 'WARNING: the darling-lol runtime is NOT signed\.' "$LOG" 2>/dev/null; then
		warning "The user runtime overlays were NOT signed; see $LOG for details."
	fi
}

resolve_rcodesign() {
	if [ -n "${RCODESIGN:-}" ] && [ -x "$RCODESIGN" ]; then
		printf '%s\n' "$RCODESIGN"
		return 0
	fi
	found=$(command -v rcodesign 2>/dev/null || true)
	if [ -n "$found" ] && [ -x "$found" ]; then
		printf '%s\n' "$found"
		return 0
	fi
	for candidate in "${HOME:-/nonexistent}/.local/bin/rcodesign" "${HOME:-/nonexistent}/.cargo/bin/rcodesign"; do
		if [ -x "$candidate" ]; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	if [ -f "$repo/tools/install-rcodesign.sh" ]; then
		sh "$repo/tools/install-rcodesign.sh" 2>/dev/null || true
	fi
}

# install-runtime.sh runs unprivileged, so a root-owned /usr/local/libexec/darling
# cannot be signed there. Sign the base SDK and refresh the trust store under sudo,
# resolving rcodesign as the user first so sudo's PATH/HOME reset cannot hide it.
sign_sdk_runtime() {
	sdk_dir=${DARLING_SDK_DIR:-/usr/local/libexec/darling}
	if [ ! -x "$repo/tools/sign-darling-runtime.sh" ] || [ ! -d "$sdk_dir" ] || [ -w "$sdk_dir" ]; then
		return 0
	fi
	# The stamp records the SDK's stat state and the signer after a successful
	# pass, so an SDK nobody touched costs neither the password nor the walk over
	# every binary. Signing rewrites each Mach-O, so an install cannot fake it.
	if python3 build/runtime/tools/sdk-sign-stamp.py --root "$repo" --sdk "$sdk_dir" check; then
		success 'Darling system components are signed'
		return 0
	fi
	if [ ! -x "$repo/tools/dc-codesign/dc-codesign" ]; then
		warning 'Base Darling SDK was NOT signed (dc-codesign is not built; install OpenSSL headers and re-run update.sh).'
		return 0
	fi
	rcodesign_bin=$(resolve_rcodesign)
	if [ -z "$rcodesign_bin" ] || [ ! -x "$rcodesign_bin" ]; then
		warning 'Base Darling SDK was NOT signed (rcodesign not found; run tools/install-rcodesign.sh and re-run update.sh).'
		return 0
	fi
	printf '\n%s%s Signing Darling system components%s\n' "$BOLD$CYAN" "$ICON_INFO" "$RESET"
	if sudo env RCODESIGN="$rcodesign_bin" sh "$repo/tools/sign-darling-runtime.sh" --sdk --quiet; then
		success 'Signing Darling system components'
		python3 build/runtime/tools/sdk-sign-stamp.py --root "$repo" --sdk "$sdk_dir" record || \
			warning 'Signing stamp not recorded; the next update signs the system components again'
	else
		warning "Base Darling SDK signing failed; re-run: sudo env RCODESIGN=\"$rcodesign_bin\" sh tools/sign-darling-runtime.sh --sdk"
	fi
}

# A container that podman still reports as running can be unreachable: after a
# host suspend or an interrupted runtime restart every `toolbox run` fails even
# though the container's init process is alive. Stopping and starting it again
# is the recovery that keeps the runtime installed inside the container.
toolbox_container_running() {
	command -v podman >/dev/null 2>&1 || return 1
	[ "$(podman container inspect --format '{{.State.Running}}' "$1" 2>/dev/null)" = true ]
}

# The PID podman recorded for a container can be stale after a suspend and
# reused by an unrelated process of this user. Only accept it while it still
# runs in the container's cgroup, or while its parent is the container's conmon.
toolbox_pid_in_container() {
	container=$1
	pid=$2
	proc=${UPDATE_PROC_ROOT:-/proc}
	id=$(podman container inspect --format '{{.Id}}' "$container" 2>/dev/null || true)
	case "$id" in
	'' | *[!0-9a-f]*) ;;
	*)
		if [ "${#id}" -eq 64 ] && grep -qF -- "$id" "$proc/$pid/cgroup" 2>/dev/null; then
			return 0
		fi
		;;
	esac
	conmon=$(podman container inspect --format '{{.State.ConmonPid}}' "$container" 2>/dev/null || true)
	case "$conmon" in
	'' | 0 | *[!0-9]*) return 1 ;;
	esac
	# The parent PID is field 4 of stat; the command name before it is in
	# parentheses and may itself contain spaces or parentheses.
	stat=$(cat "$proc/$pid/stat" 2>/dev/null) || return 1
	rest=${stat##*\)}
	rest=${rest# }
	rest=${rest#* }
	ppid=${rest%% *}
	[ "$ppid" = "$conmon" ] || return 1
	comm=$(cat "$proc/$conmon/comm" 2>/dev/null) || return 1
	[ "$comm" = conmon ]
}

restart_toolbox_container() {
	container=$1
	status=0
	stop_toolbox_podman "$container" || status=$?
	if [ "$status" -eq 1 ]; then
		return 1
	fi
	attempt=0
	until podman start "$container" >/dev/null 2>&1; do
		attempt=$((attempt + 1))
		if [ "$attempt" -ge 15 ]; then
			warning "Podman could not start Toolbox container '$container'."
			return 0
		fi
		sleep 1
	done
}

# Rootless podman signals the container through its runtime. When that pidfd
# signal is refused (`operation not permitted`, seen when a container's user
# namespace outlived a suspend) the host can still signal the recorded init
# PID, as `podman stop` would, once that PID is confirmed to belong to the
# container. Exit status: 0 when the stop was attempted, 1 when the recorded
# PID is not the container's, and 2 when `podman stop` failed and no usable PID
# could be found.
stop_toolbox_podman() {
	container=$1
	podman stop --time 10 "$container" >/dev/null 2>&1 && return 0
	pid=$(podman container inspect --format '{{.State.Pid}}' "$container" 2>/dev/null || true)
	case "$pid" in
	'' | 0 | *[!0-9]*) return 2 ;;
	esac
	if ! toolbox_pid_in_container "$container" "$pid"; then
		warning "Podman could not stop Toolbox container '$container', and PID $pid no longer belongs to it; not signaling it."
		return 1
	fi
	warning "Podman could not stop Toolbox container '$container'; signaling PID $pid."
	kill -TERM "$pid" 2>/dev/null || true
	attempt=0
	while kill -0 "$pid" 2>/dev/null && [ "$attempt" -lt 20 ]; do
		sleep 1
		attempt=$((attempt + 1))
	done
	return 0
}

# A container this update started or restarted is not needed once the update
# ends, so take it back down instead of leaving conmon and the container init
# running. A container that was already reachable belongs to a session the
# user opened, and the caller never asks for it to be stopped.
stop_toolbox_container() {
	container=$1
	command -v podman >/dev/null 2>&1 || return 0
	info "Stopping Toolbox container '$container'..."
	status=0
	stop_toolbox_podman "$container" || status=$?
	if [ "$status" -eq 0 ]; then
		success "Stopped Toolbox container '$container'"
		return 0
	fi
	warning "Podman could not stop Toolbox container '$container'; it is still running."
	return 1
}

# The install steps shut Darling down and stop leftover Riot processes, so
# refuse rather than end a session. The same probe guards a container restart,
# which would end a session still running inside an unreachable container.
# Exit status: 0 when a client runs, 1 when the scan completed without one, and
# 2 when the probe itself failed, so a failed probe is never read as "no
# clients". The scan prints its result, and only that token with a zero exit
# yields status 1; an interpreter that fails before the probe body cannot.
riot_or_league_running() {
	command -v python3 >/dev/null 2>&1 || return 2
	# UPDATE_PROC_ROOT exists for the tests, like CLEAN_PREFIX_PROC_ROOT.
	# -I keeps an inherited PYTHONHOME/PYTHONPATH from breaking interpreter
	# startup before the scan runs.
	probe_output=$(python3 -I - <<'PY'
import os
import sys
from pathlib import Path


def main():
    uid = os.getuid()
    for entry in Path(os.environ.get('UPDATE_PROC_ROOT') or '/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command_line = (entry / 'cmdline').read_bytes()
        except FileNotFoundError:
            # The process exited while the scan ran; nothing left to check.
            continue
        except OSError:
            # On a host with a restricted /proc the entries of other users
            # cannot be read, and the clients run as this user. An entry of
            # ours that cannot be read fails the scan instead of counting as
            # "no client".
            try:
                owner = entry.stat().st_uid
            except FileNotFoundError:
                continue
            except OSError:
                return 2
            if owner == uid:
                return 2
            continue
        if (b'/Riot Client.app/' in command_line or
                b'/League of Legends.app/' in command_line):
            print('running')
            return 0
    print('none')
    return 0


try:
    sys.exit(main())
except Exception:
    sys.exit(2)
PY
) || return 2
	case "$probe_output" in
	running) return 0 ;;
	none) return 1 ;;
	*) return 2 ;;
	esac
}

reject_running_clients() {
	probe_status=0
	riot_or_league_running || probe_status=$?
	case "$probe_status" in
	0)
		error 'A Riot Client or League process is running; exit the game and both clients, then run this again.'
		exit 1
		;;
	1) ;;
	*)
		error 'Could not check for a running Riot Client or League process; refusing to update.'
		exit 1
		;;
	esac
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
	run 'Building Darling-LoL' ./scripts/build.sh
	run 'Stopping the running runtime' python3 build/runtime/tools/stop-darling-runtime.py
	install_system_components cmake --install build/darling-src/build
	run 'Recording the installed base runtime' python3 scripts/nix-state.py stamp-base
	run 'Installing the user runtime' ./scripts/install-runtime.sh
	warn_if_runtime_unsigned
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
is_toolbx_cli() {
	command -v toolbox >/dev/null 2>&1 || return 1
	case "$(toolbox --version </dev/null 2>/dev/null || true)" in
	'toolbox version '*) return 0 ;;
	*) return 1 ;;
	esac
}

distrobox_container_exists() {
	command -v podman >/dev/null 2>&1 || return 1
	podman container exists -- "$1" >/dev/null 2>&1 || return 1
	[ "$(podman container inspect --format '{{index .Config.Labels "manager"}}' "$1" 2>/dev/null || true)" = distrobox ]
}

inside_container=0
inside_backend=''
# Distrobox comes first: its init also creates /run/.toolboxenv, so only
# CONTAINER_ID (set by distrobox create/enter) tells the two apart.
if [ -n "${CONTAINER_ID:-}" ] && [ -f "${DARLING_CONTAINER_ENV:-/run/.containerenv}" ]; then
	inside_container=1
	inside_backend=distrobox
elif [ -f "${DARLING_TOOLBOX_ENV:-/run/.toolboxenv}" ]; then
	inside_container=1
	inside_backend=toolbox
fi
if [ "$inside_container" = 0 ] && [ -z "${DARLING_LOL_UPDATE_REEXEC:-}" ] &&
	[ ! -f build/native-install ]; then
	name=''
	backend=''
	if [ -f build/toolbox-container ]; then
		name=$(cat build/toolbox-container)
		backend=toolbox
		if [ -r build/toolbox-backend ] && [ "$(cat build/toolbox-backend)" = distrobox ]; then
			backend=distrobox
		fi
	else
		# $HOME holds a single launcher entry shared by every checkout: only trust
		# its container name when its Exec points at this checkout's scripts.
		# Reserved path characters come out escaped in the Exec value, so those
		# paths may not match here; a false negative is safe.
		entry="${HOME:-}/.local/share/applications/darling-launcher.desktop"
		name=$(grep '^Exec=' "$entry" 2>/dev/null | grep -F -- "$repo/scripts/" |
			sed -n 's/^Exec=toolbox run -c \([^ ]*\) .*$/\1/p' | head -n 1)
		if [ -n "$name" ]; then
			backend=toolbox
		else
			name=$(grep '^Exec=' "$entry" 2>/dev/null | grep -F -- "$repo/scripts/" |
				sed -n 's/^Exec=\(env DBX_CONTAINER_MANAGER=podman \)\{0,1\}distrobox enter --no-tty --name \([^ ]*\) -- .*$/\2/p' | head -n 1)
			if [ -n "$name" ]; then
				backend=distrobox
			fi
		fi
	fi
	# A container this run starts or restarts is stopped again when the update
	# ends. One that is already reachable stays up: it belongs to a session the
	# user opened, and stopping it would end that shell.
	stop_container=0
	if [ -n "$name" ]; then
		toolbox_container_running "$name" || stop_container=1
	fi
	# No record of any kind: only a legacy checkout can be assumed to live in the
	# installer's default container. This probe also starts a stopped container,
	# so it must not decide for checkouts that are already recorded above.
	if [ -z "$name" ]; then
		if is_toolbx_cli; then
			if toolbox_container_running darling-lol; then
				stop_container=0
			else
				stop_container=1
			fi
			if toolbox run -c darling-lol sh -c 'command -v darling' >/dev/null 2>&1; then
				name=darling-lol
				backend=toolbox
			fi
		fi
		if [ -z "$name" ] && command -v distrobox >/dev/null 2>&1 &&
			distrobox_container_exists darling-lol; then
			if toolbox_container_running darling-lol; then
				stop_container=0
			else
				stop_container=1
			fi
			if [ "$stop_container" -eq 1 ]; then
				TOOLBOX_STOP=darling-lol
			fi
			if DBX_CONTAINER_MANAGER=podman distrobox enter --no-tty --name darling-lol -- sh -c 'command -v darling' </dev/null >/dev/null 2>&1; then
				name=darling-lol
				backend=distrobox
			fi
		fi
	fi
	if [ -n "$name" ]; then
		container_label=Toolbox
		enter_cmd="toolbox enter $name"
		if [ "$backend" = distrobox ]; then
			container_label=Distrobox
			enter_cmd="distrobox enter $name"
			if ! command -v distrobox >/dev/null 2>&1; then
				error "This checkout was installed via Distrobox container '$name', but Distrobox"
				printf '  is not installed on this host. Install Distrobox, then run the update\n' >&2
				printf '  inside the container: %s\n' "$enter_cmd" >&2
				exit 1
			fi
			if ! distrobox_container_exists "$name"; then
				error "This checkout was installed via Distrobox container '$name', but that container does not exist."
				printf '  Recreate it or reinstall with scripts/install-native.sh --toolbox.\n' >&2
				exit 1
			fi
		elif ! command -v toolbox >/dev/null 2>&1; then
			error "This checkout was installed via Toolbox container '$name', but Toolbox"
			printf '  is not installed on this host. Install Toolbox, then run the update\n' >&2
			printf '  inside the container: %s\n' "$enter_cmd" >&2
			exit 1
		fi
		if [ "$stop_container" = 1 ]; then
			TOOLBOX_STOP=$name
		fi
		container_probe() {
			if [ "$backend" = distrobox ]; then
				distrobox_container_exists "$1" &&
					DBX_CONTAINER_MANAGER=podman distrobox enter --no-tty --name "$1" -- true </dev/null 2>/dev/null
			else
				toolbox run -c "$1" true 2>/dev/null
			fi
		}
		# `toolbox run` / `distrobox enter` starts a stopped container, but that start
		# can still fail transiently (for example while another session is shutting
		# down). A container that podman reports as running yet stays unreachable needs
		# a stop/start instead, because re-entry alone cannot recover it. Ask before
		# giving up, defaulting to the fix.
		if ! container_probe "$name"; then
			if toolbox_container_running "$name"; then
				state='running but not reachable'
				action='Restart'
			else
				state='not reachable'
				action='Start'
			fi
			printf '\n%s%s%s %s container %s is %s. %s it and continue the update? [Y/n] ' \
				"$YELLOW" "$ICON_WARN" "$RESET" "$container_label" "'$name'" "$state" "$action"
			reply=''
			# Only a terminal can answer. Pipes and a closed stdin keep the
			# default, so a non-interactive update never blocks here.
			if [ -t 0 ]; then
				read -r reply || reply=''
			fi
			case "$reply" in
			n | N | no | No | NO)
				if [ "$action" = Restart ]; then
					error "This checkout was installed via $container_label container '$name', but that container is"
					printf '  running but unreachable. Restart it and run the update inside it: podman restart %s\n' "$name" >&2
				else
					error "This checkout was installed via $container_label container '$name', but that container is not"
					printf '  reachable. Start it and run the update inside it: %s\n' "$enter_cmd" >&2
				fi
				exit 1
				;;
			esac
			if [ "$action" = Restart ]; then
				probe_status=0
				riot_or_league_running || probe_status=$?
				if [ "$probe_status" -eq 0 ]; then
					error 'A Riot Client or League process is running; exit the game and both clients, then run this again.'
					printf '  %s container %s was left untouched.\n' "$container_label" "'$name'" >&2
					exit 1
				elif [ "$probe_status" -ne 1 ]; then
					error 'Could not check for a running Riot Client or League process; refusing to restart.'
					printf '  The container was left untouched. Restart it by hand: podman restart %s\n' "$name" >&2
					exit 1
				fi
			fi
			if [ "$action" = Restart ]; then
				info "Restarting $container_label container '$name'..."
				if ! restart_toolbox_container "$name"; then
					error "$container_label container '$name' could not be stopped."
					printf '  Stop it by hand, then run the update again: podman stop %s\n' "$name" >&2
					exit 1
				fi
			else
				info "Starting $container_label container '$name'..."
			fi
			attempt=0
			until container_probe "$name"; do
				attempt=$((attempt + 1))
				if [ "$attempt" -ge 10 ]; then
					error "$container_label container '$name' could not be started."
					if [ "$action" = Restart ]; then
						printf '  Restart it and run the update inside it: podman restart %s\n' "$name" >&2
					else
						printf '  Start it and run the update inside it: %s\n' "$enter_cmd" >&2
					fi
					exit 1
				fi
				sleep 1
			done
			# This run recovered the container, so it stops it again below.
			stop_container=1
		fi
		info "$container_label install detected; running the update inside container '$name'..."
		if [ "$stop_container" = 1 ]; then
			TOOLBOX_STOP=$name
		fi
		# Keep the inner run's status: a failed update must not look successful
		# here, and the EXIT trap stops the container either way.
		rc=0
		if [ "$backend" = distrobox ]; then
			set -- env DARLING_LOL_UPDATE_REEXEC=1 sh "$repo/scripts/update.sh"
			if [ "$VERBOSE" = 1 ]; then
				set -- "$@" --verbose
			fi
			if [ -t 0 ]; then
				DBX_CONTAINER_MANAGER=podman distrobox enter --name "$name" -- "$@" || rc=$?
			else
				DBX_CONTAINER_MANAGER=podman distrobox enter --no-tty --name "$name" -- "$@" || rc=$?
			fi
		elif [ "$VERBOSE" = 1 ]; then
			toolbox run --container "$name" env DARLING_LOL_UPDATE_REEXEC=1 \
				sh "$repo/scripts/update.sh" --verbose || rc=$?
		else
			toolbox run --container "$name" env DARLING_LOL_UPDATE_REEXEC=1 \
				sh "$repo/scripts/update.sh" || rc=$?
		fi
		exit "$rc"
	fi
fi

# The re-exec lands here inside the container: say so, and name it when the
# install marker that selected it is still in the checkout.
if [ "$inside_container" = 1 ]; then
	name=''
	if [ -f build/toolbox-container ]; then
		IFS= read -r name <build/toolbox-container || true
	elif [ "$inside_backend" = distrobox ] && [ -n "${CONTAINER_ID:-}" ]; then
		name=$CONTAINER_ID
	fi
	if [ -r build/toolbox-backend ] && [ "$(cat build/toolbox-backend)" = distrobox ]; then
		inside_backend=distrobox
	fi
	if [ "$inside_backend" = distrobox ]; then
		if [ -n "$name" ]; then
			info "Running inside Distrobox container '$name'."
		else
			info 'Running inside a Distrobox container.'
		fi
	elif [ -n "$name" ]; then
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
reject_running_clients

if ! command -v sudo >/dev/null 2>&1; then
	error 'sudo is required to install the system components.'
	exit 1
fi
prepare_output
printf '\n%s%sUpdating Darling-LoL%s\n' "$BOLD" "$CYAN" "$RESET"

run 'Building Darling-LoL' ./scripts/build.sh
run 'Stopping the running runtime' python3 build/runtime/tools/stop-darling-runtime.py
install_system_components sudo cmake --install build/darling-src/build
run 'Installing the user runtime' ./scripts/install-runtime.sh
warn_if_runtime_unsigned
sign_sdk_runtime
replace_shared_identity
show_log
success 'Update installed. Start Darling with: sh scripts/run.sh'
