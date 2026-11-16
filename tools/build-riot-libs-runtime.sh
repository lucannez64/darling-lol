#!/bin/sh
# Build every overlay of the optimized library batch, in order.
#
# Each builder recompiles the component's own objects at -O3 -march=native -mtune=generic with the flags
# the configured build gives them and relinks with the configured link line;
# none of them runs a full cmake build. The builders stop while a League game
# process is running, because a compile job would skew the FPS the user is
# measuring. Install the artifacts with
# python3 build/runtime/tools/install-riot-libs-runtime.py.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

# The three builders are independent (distinct object prefixes, folders and
# outputs), so run them concurrently. Each builder fingerprints its inputs and
# exits immediately when everything is up to date, so on an unchanged tree all
# three return at once. wait is called once per pid: a bare `wait` with no
# arguments swallows individual exit statuses, which set -e must see.
python3 tools/build-riot-libc-runtime.py &
libc_pid=$!
python3 tools/build-riot-cxx-runtime.py &
cxx_pid=$!
python3 tools/build-riot-platform-strings.py &
platform_pid=$!
status=0
for pid in "$libc_pid" "$cxx_pid" "$platform_pid"; do
  wait "$pid" || status=1
done
exit "$status"
