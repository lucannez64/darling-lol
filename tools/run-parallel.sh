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

pids=''
count=0
status=0
wait_jobs() {
	for pid in $pids; do
		wait "$pid" || status=1
	done
	pids=''
	count=0
}

while IFS= read -r line; do
	case $line in '' | '#'*) continue ;; esac
	sh -c "$line" </dev/null &
	pids="$pids $!"
	count=$((count + 1))
	[ "$count" -lt "$jobs" ] || wait_jobs
done
wait_jobs
exit "$status"
