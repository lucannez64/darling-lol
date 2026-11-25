#!/usr/bin/env python3
"""Skip the explicit CMake configure when nothing it reads changed.

`scripts/build.sh` runs `cmake -S build/darling-src -B .../build` before every
build. On this tree that configure pass costs about seven seconds even when it
changes nothing, and it is only needed when the prepared sources, the flags in
`scripts/build.sh` or the cache changed.

Make re-runs CMake by itself when a `CMakeLists.txt` is newer than the cache,
but not when a patch adds a file a `file(GLOB)` would pick up, so the stamp
covers the whole prepared checkout instead of relying on timestamps.

`record` stores the digests after a successful configure; `check` recomputes
them and exits 0 when the configure step can be skipped. A missing cache, an
unreadable checkout or an unknown section is always a change.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console import fail, ok  # noqa: E402
from checkout_digest import digest as checkout_digest  # noqa: E402
from file_digest import sha256  # noqa: E402

VERSION = 1

# Inputs of the explicit configure: the prepared checkout, the script that
# spells out the flags, and the cache the last configure produced.
SCRIPT = 'scripts/build.sh'
CACHE = 'build/darling-src/build/CMakeCache.txt'


def script_digest(root):
    path = Path(root) / SCRIPT
    try:
        return sha256(path)
    except OSError:
        return None


def cache_digest(root):
    path = Path(root) / CACHE
    try:
        return sha256(path)
    except OSError:
        return None


def collect(root):
    return {
        'tree': checkout_digest(root),
        'script': script_digest(root),
        'cache': cache_digest(root),
    }


def default_root():
    here = Path(__file__).resolve().parent
    for candidate in (here,) + here.parents:
        if ((candidate / 'scripts/build.sh').is_file()
                and (candidate / 'tools/checkout_digest.py').is_file()):
            return candidate
    return here.parent


def record(state, sections):
    unknown = sorted(name for name, value in sections.items() if value is None)
    if unknown:
        fail('configure stamp: cannot record %s; the next build reconfigures'
             % ', '.join(unknown))
        return 1
    state.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': VERSION,
        'recorded': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'sections': sections,
    }
    part = state.with_name(state.name + '.part')
    part.write_text(json.dumps(payload, indent=1, sort_keys=True) + '\n')
    os.replace(part, state)
    return 0


def check(state, sections):
    try:
        recorded = json.loads(Path(state).read_text())
    except (OSError, ValueError):
        print('configure stamp: no usable state at %s' % state)
        return 1
    if not isinstance(recorded, dict) or recorded.get('version') != VERSION:
        print('configure stamp: stale format at %s' % state)
        return 1
    previous = recorded.get('sections')
    if not isinstance(previous, dict):
        print('configure stamp: missing sections at %s' % state)
        return 1
    changed = []
    for name in sorted(sections):
        if sections[name] is None:
            changed.append(name + ' (unreadable)')
        elif previous.get(name) != sections[name]:
            changed.append(name)
    if changed:
        print('configure stamp: changed %s' % ', '.join(changed))
        return 1
    ok('configure stamp: up to date')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('check', 'record'))
    parser.add_argument('--root', default=None,
                        help='repository root (default: derived from this file)')
    parser.add_argument('--state', default=None,
                        help='stamp file (default: build/configure-stamp.json)')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    state = Path(args.state) if args.state else root / 'build/configure-stamp.json'
    sections = collect(root)
    if args.command == 'record':
        return record(state, sections)
    return check(state, sections)


if __name__ == '__main__':
    sys.exit(main())
