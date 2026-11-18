#!/usr/bin/env python3
"""Build, gate, install and verify the process-name fix in one command.

The process-name repair (mldr -> executable-path names, #245) lives in
Darling's libsystem_kernel emulation, so reaching the running client needs
three things: a rebuilt dylib, a copy into the prefix's RuntimeLibraries
override (the launcher loads every client with DYLD_LIBRARY_PATH pointing
there), and a session restart. This sequences all of it:

  1. build    the system_kernel target from the patched build tree
  2. gate     tools/run-process-name-check.py --kernel on the candidate,
              in a throwaway private prefix, against a copy of the
              prefix's current runtime
  3. stop     the configured prefix's Darling session -- host writes are
              invisible to a mounted overlay, so the copy must happen
              with the session down
  4. copy     the dylib into <prefix>/Users/<guest>/RuntimeLibraries/
              (an existing override is kept as *.before-process-name-fix)
  5. verify   tools/run-process-name-check.py against the installed runtime

Nothing touches the runtime unless the gate passes, and the game prefix is
never used for probing: both probe runs use throwaway private prefixes.
After this succeeds, the next Riot Client launch reports macOS-style process
names; the tamper report no longer stages a "mldr" parent name.

Usage:
    python3 tools/deploy-process-name-fix.py [--jobs N] [--keep-build]
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
config = json.loads((root / 'build/config.json').read_text())
prefix = Path(config['prefix']).resolve()
guest = config.get('guest_user', os.environ['USER'])
runtime = (prefix / 'Users' / guest).resolve()
libraries = runtime / 'RuntimeLibraries'
built = (root / 'build/darling-src/build/src/external/xnu/darling/src'
         '/libsystem_kernel/libsystem_kernel.dylib').resolve()

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--jobs', type=int, default=min(4, os.cpu_count() or 2),
                    help='parallel build jobs (default: 4 or cpu count)')
args = parser.parse_args()


def run(command, *, env=None, capture=False):
    print('$', ' '.join(str(c) for c in command), flush=True)
    return subprocess.run([str(c) for c in command], env=env, check=True,
                          capture_output=capture, text=True)


print(f'prefix:   {prefix}')
print(f'runtime:  {runtime}')
if not libraries.is_dir():
    sys.exit(f'no RuntimeLibraries in {runtime}; run scripts/install-runtime.sh first')

# 1. Build the patched kernel from the checked-out overlay source.
run(['cmake', '--build', root / 'build/darling-src/build',
     '--target', 'system_kernel', '--parallel', args.jobs])
if not built.is_file():
    sys.exit(f'build did not produce {built}')

# 2. Gate the candidate in a private prefix against a copy of this runtime.
run([sys.executable, root / 'tools/run-process-name-check.py',
     '--runtime', runtime, '--kernel', built])

# 3. Stop the prefix session: host writes are invisible to a mounted overlay.
run([sys.executable, root / 'build/runtime/tools/stop-darling-runtime.py'],
    env={**os.environ, 'DPREFIX': str(prefix)})

# 4. Install, keeping any previous override.
destination = libraries / 'libsystem_kernel.dylib'
if destination.exists():
    backup = libraries / 'libsystem_kernel.dylib.before-process-name-fix'
    shutil.copy2(destination, backup)
    print(f'kept previous override as {backup.name}')
shutil.copy2(built, destination)

# 5. Verify what the client will actually load.
run([sys.executable, root / 'tools/run-process-name-check.py',
     '--runtime', runtime])

print('done: the next Riot Client launch loads the fixed libsystem_kernel '
      'from RuntimeLibraries and reports macOS-style process names.')
