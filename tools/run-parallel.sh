#!/bin/sh
# Run one shell command per input line, at most RCLOL_PARALLEL_JOBS at a time.
#
# The overlay builders are independent jobs whose output does not need to be
# ordered, so install-riot-gpu-runtime.sh feeds them through here instead of
# compiling one after another. Each line is run by sh, so shell syntax works;
# empty lines and comments are skipped. Any failing job makes the run fail after
# every started job has been reaped, because a bare wait would swallow the
# individual exit statuses that set -e must see.
set -eu

jobs=${RCLOL_PARALLEL_JOBS:-$(nproc 2>/dev/null || echo 4)}
case $jobs in '' | 0 | *[!0-9]*) jobs=$(nproc 2>/dev/null || echo 4) ;; esac
case $jobs in *[!0]*) ;; *) jobs=4 ;; esac

# Each job runs in its own process group, so a cancelled pool can stop the
# whole builder tree (python and the compilers it started), not only its sh.
if command -v setsid >/dev/null 2>&1; then group=setsid; else group=; fi

pids=''
count=0
status=0
wait_jobs() {
	for pid in $pids; do
		code=0
		wait "$pid" || code=$?
		if [ "$code" -ne 0 ]; then
			status=1
			eval "failed=\$cmd_$pid"
			printf 'run-parallel.sh: exit %s: %s\n' "$code" "$failed" >&2
		fi
	done
	pids=''
	count=0
}

stop_jobs() {
	trap - INT TERM HUP
	for pid in $pids; do
		# No `--`: dash's kill reads it as a pid once a signal is given.
		if [ -n "$group" ]; then kill -TERM "-$pid" 2>/dev/null || :; fi
		kill -TERM "$pid" 2>/dev/null || :
	done
	for pid in $pids; do wait "$pid" 2>/dev/null || :; done
	exit "$1"
}
trap 'stop_jobs 130' INT
trap 'stop_jobs 143' TERM
trap 'stop_jobs 129' HUP

while IFS= read -r line; do
	case $line in '' | '#'*) continue ;; esac
	$group sh -c "$line" </dev/null &
	pids="$pids $!"
	eval "cmd_$!=\$line"
	count=$((count + 1))
	[ "$count" -lt "$jobs" ] || wait_jobs
done
wait_jobs
exit "$status"
