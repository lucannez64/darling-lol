#!/usr/bin/env python3
"""Skip the base SDK signing pass when the installed SDK is already signed.

`update.sh` signs `/usr/local/libexec/darling` under sudo after every update.
Even when every binary is already signed the pass walks 33 000 files and asks
`dc-codesign` about each Mach-O, which costs about five seconds and a sudo
prompt on an SDK nobody touched.

`record` stores, after a successful pass, the signer's digest and a stat digest
of the SDK: relative path, size, modification time and mode for every regular
file, and the target of every symlink. `check` recomputes it; exit 0 means the
SDK is exactly what the last pass left behind and the pass can be skipped.

The digest uses size and modification time instead of content because parts of
the SDK are not readable without root. That is enough here: signing rewrites
every Mach-O, so an install that puts unsigned binaries back cannot reproduce
the recorded timestamps. Anything unreadable is a change, never a skip.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console import fail, note, ok  # noqa: E402
from file_digest import sha256  # noqa: E402

VERSION = 1

DEFAULT_SDK = '/usr/local/libexec/darling'
SIGNER = 'tools/sign-darling-runtime.sh'


def _walk(folder, relative, hasher):
    """Hash one directory level, sorted, recursing into real directories."""
    with os.scandir(folder) as scan:
        entries = sorted(scan, key=lambda item: item.name)
    for item in entries:
        path = relative + item.name
        if item.is_symlink():
            hasher.update(('%s\0link\0%s\n' % (path, os.readlink(item.path))).encode())
        elif item.is_dir(follow_symlinks=False):
            _walk(item.path, path + '/', hasher)
        else:
            info = item.stat(follow_symlinks=False)
            hasher.update(('%s\0file\0%d\0%d\0%d\n' % (
                path, info.st_size, info.st_mtime_ns, info.st_mode)).encode())


def sdk_digest(sdk):
    """Stat digest of one SDK tree; None when any entry cannot be read."""
    root = Path(sdk)
    if not root.is_dir():
        return None
    hasher = hashlib.sha256()
    try:
        _walk(str(root), '', hasher)
    except OSError:
        return None
    return hasher.hexdigest()


def signer_digest(root):
    try:
        return sha256(Path(root) / SIGNER)
    except OSError:
        return None


def collect(root, sdk):
    return {
        'sdk': sdk_digest(sdk),
        'signer': signer_digest(root),
    }


def default_root():
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if ((candidate / 'scripts/update.sh').is_file()
                and (candidate / SIGNER).is_file()):
            return candidate
    return here.parent


def record(state, sections):
    unknown = sorted(name for name, value in sections.items() if value is None)
    if unknown:
        fail('sdk signing stamp: cannot record %s; the next update signs again'
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
        note('sdk signing stamp: no usable state at %s' % state)
        return 1
    if not isinstance(recorded, dict) or recorded.get('version') != VERSION:
        note('sdk signing stamp: stale format at %s' % state)
        return 1
    previous = recorded.get('sections')
    if not isinstance(previous, dict):
        note('sdk signing stamp: missing sections at %s' % state)
        return 1
    changed = []
    for name in sorted(sections):
        if sections[name] is None:
            changed.append(name + ' (unreadable)')
        elif previous.get(name) != sections[name]:
            changed.append(name)
    if changed:
        note('sdk signing stamp: changed %s' % ', '.join(changed))
        return 1
    ok('sdk signing stamp: up to date')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('check', 'record'))
    parser.add_argument('--root', default=None,
                        help='repository root (default: derived from this file)')
    parser.add_argument('--sdk', default=None,
                        help='installed SDK (default: DARLING_SDK_DIR or %s)' % DEFAULT_SDK)
    parser.add_argument('--state', default=None,
                        help='stamp file (default: build/sdk-sign-stamp.json)')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    sdk = args.sdk or os.environ.get('DARLING_SDK_DIR') or DEFAULT_SDK
    state = Path(args.state) if args.state else root / 'build/sdk-sign-stamp.json'
    sections = collect(root, sdk)
    if args.command == 'record':
        return record(state, sections)
    return check(state, sections)


if __name__ == '__main__':
    sys.exit(main())
