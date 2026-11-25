#!/usr/bin/env python3
"""Skip `cmake --install` when the built tree and the installed SDK are unchanged.

`update.sh` installs the base Darling runtime with `cmake --install
build/darling-src/build` after every build. The install copies about 33 000
files (over half a gigabyte) and re-evaluates every rule even when nothing
changed, which costs around 13 seconds and, outside the container, a sudo
prompt.

`record` stores, after a successful install, the digest of the build outputs,
the install manifest and the presence of every path the manifest names. `check`
recomputes them; exit 0 means the installed SDK already matches the build tree
and the install can be skipped.

The build outputs are hashed by content (about 400 MB, well under a second from
the page cache) because `cmake --build` relinks without touching every file.
`CMakeFiles` holds intermediate objects and is excluded: nothing is installed
from there, and hashing it would cost several seconds. Anything missing or
unreadable is a change, never a skip.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checkout_digest import digest as checkout_digest  # noqa: E402
from console import fail, note, ok  # noqa: E402
from file_digest import sha256  # noqa: E402

VERSION = 1

BUILD = 'build/darling-src/build'
MANIFEST = 'install_manifest.txt'
SKIP_DIRS = ('CMakeFiles',)


def _reraise(error):
    raise error


def outputs_digest(root):
    """Digest of every regular file the install rules read, by content."""
    base = Path(root) / BUILD
    if not base.is_dir():
        return None
    hasher = hashlib.sha256()
    try:
        for top, dirs, files in os.walk(base, onerror=_reraise):
            dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
            for name in sorted(files):
                path = Path(top) / name
                if not path.is_file() or path.is_symlink():
                    continue
                hasher.update(path.relative_to(base).as_posix().encode())
                hasher.update(b'\0')
                hasher.update(sha256(path).encode())
                hasher.update(b'\n')
    except OSError:
        return None
    return hasher.hexdigest()


def manifest_paths(root):
    try:
        text = (Path(root) / BUILD / MANIFEST).read_text()
    except (OSError, ValueError):
        return None
    return [line for line in text.splitlines() if line]


def manifest_digest(paths):
    if paths is None:
        return None
    return hashlib.sha256('\n'.join(paths).encode()).hexdigest()


def destinations_digest(paths):
    """Digest of every installed path's presence.

    `cmake --install` moves a file away (`install-bin`), so the record has to
    accept the paths that legitimately do not exist after a successful install
    and still notice one that disappeared later.
    """
    if paths is None:
        return None
    hasher = hashlib.sha256()
    for path in paths:
        hasher.update(path.encode())
        hasher.update(b'\0')
        hasher.update(b'1' if os.path.lexists(path) else b'0')
        hasher.update(b'\n')
    return hasher.hexdigest()


def collect(root):
    paths = manifest_paths(root)
    return {
        # install(FILES ...) rules read plists and scripts straight from the
        # checkout, so a patch that changes one has to reinstall even though the
        # build outputs and the manifest are identical.
        'tree': checkout_digest(root),
        'outputs': outputs_digest(root),
        'manifest': manifest_digest(paths),
        'destinations': destinations_digest(paths),
    }


def default_root():
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if ((candidate / 'scripts/update.sh').is_file()
                and (candidate / 'tools/checkout_digest.py').is_file()):
            return candidate
    return here.parent


def record(state, sections, paths):
    unknown = sorted(name for name, value in sections.items() if value is None)
    if unknown:
        fail('install stamp: cannot record %s; the next update reinstalls'
             % ', '.join(unknown))
        return 1
    state.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': VERSION,
        'recorded': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'installed': len(paths),
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
        note('install stamp: no usable state at %s' % state)
        return 1
    if not isinstance(recorded, dict) or recorded.get('version') != VERSION:
        note('install stamp: stale format at %s' % state)
        return 1
    previous = recorded.get('sections')
    if not isinstance(previous, dict):
        note('install stamp: missing sections at %s' % state)
        return 1
    changed = []
    for name in sorted(sections):
        if sections[name] is None:
            changed.append(name + ' (unreadable)')
        elif previous.get(name) != sections[name]:
            changed.append(name)
    if changed:
        note('install stamp: changed %s' % ', '.join(changed))
        return 1
    ok('install stamp: up to date')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('check', 'record'))
    parser.add_argument('--root', default=None,
                        help='repository root (default: derived from this file)')
    parser.add_argument('--state', default=None,
                        help='stamp file (default: build/install-stamp.json)')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    state = Path(args.state) if args.state else root / 'build/install-stamp.json'
    sections = collect(root)
    if args.command == 'record':
        return record(state, sections, manifest_paths(root) or [])
    return check(state, sections)


if __name__ == '__main__':
    sys.exit(main())
