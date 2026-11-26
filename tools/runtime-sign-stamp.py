#!/usr/bin/env python3
"""Skip the per-user runtime signing pass when nothing it signs has changed.

`install-runtime.sh` runs `sign-darling-runtime.sh --prefix` on every update: it
walks the overlay trees, asks dc-codesign about every Mach-O and verifies the
signature. Files that already carry one are skipped, but the walk still costs
time and repairs nothing when nothing changed. A writable SDK makes that pass
`--sdk --prefix`, which also walks the ~33 000 files of the installed SDK.

`record` stores, after a successful pass, the signer's digest and a stat digest
of every root the pass covered: relative path, size, modification time and mode
for every regular file, the target of every symlink, and the real path of the
root. `check` recomputes them; exit 0 means the trees are exactly what the last
pass left behind and the pass can be skipped. Signing rewrites every Mach-O, and
installs, syncs and rebuilds write new files, so none of them can reproduce the
recorded state. Anything unreadable is a change, never a skip. The canonical
mirror leaves a signed copy alone because signing is deterministic and both
copies end up identical; if that ever changed, the rewrite would reopen the
stamp and only cost the pass it already costs today.

With `--sdk` the pass also writes the local trust store, so the stamp covers the
store's content too: a removed or replaced store signs again. A root-owned SDK
is signed by `update.sh` instead and has its own stamp (tools/sdk-sign-stamp.py).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console import fail, note, ok  # noqa: E402
from file_digest import sha256  # noqa: E402

VERSION = 1

SIGNER = 'tools/sign-darling-runtime.sh'
TRUSTCACHE = 'tools/dc-trustcache.py'
DEFAULT_SDK = '/usr/local/libexec/darling'

# The roots sign-darling-runtime.sh --prefix selects, in the same order. The
# signer's digest is part of the stamp, so adding a root there opens the gate
# even when this list is not updated in the same commit.
PREFIX_ROOTS = (
    'Users/*/RuntimeLibraries',
    'Users/*/RuntimeFrameworks',
    'usr/lib',
    'System/Library/Frameworks',
    'Users/*/Library/Audio/Plug-Ins/Components',
    'Users/*/libDarlingXShm.dylib',
    'Users/*/libDarlingWayland.dylib',
)


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


def root_digest(root):
    """Stat digest of one signing root; None when any entry cannot be read."""
    root = str(root)
    hasher = hashlib.sha256()
    # The real path catches a root that now points at a different tree, such as
    # a workspace symlink that was re-pointed while the trees look alike.
    hasher.update(('root\0%s\0%s\n' % (root, os.path.realpath(root))).encode())
    try:
        if os.path.isfile(root) and not os.path.islink(root):
            info = os.stat(root, follow_symlinks=False)
            hasher.update(('file\0%d\0%d\0%d\n' % (
                info.st_size, info.st_mtime_ns, info.st_mode)).encode())
        elif os.path.islink(root) and not os.path.isdir(root):
            hasher.update(('link\0%s\n' % os.readlink(root)).encode())
        else:
            _walk(root, '', hasher)
    except OSError:
        return None
    return hasher.hexdigest()


def prefix_roots(prefix):
    """The existing overlay roots the signer would walk, in its order."""
    found = []
    for pattern in PREFIX_ROOTS:
        for path in sorted(Path(prefix).glob(pattern)):
            if path.is_dir() or path.is_file():
                found.append(path)
    return found


def signer_digest(root):
    try:
        return sha256(Path(root) / SIGNER)
    except OSError:
        return None


def trust_store(root):
    """The default store dc-trustcache uses for this user, or None."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(root) / TRUSTCACHE), 'default-store'],
            capture_output=True, text=True)
    except OSError:
        return None
    path = result.stdout.strip()
    return path if result.returncode == 0 and path else None


def collect(root, prefix, sdk=None):
    sections = {'signer': signer_digest(root), 'prefix': str(prefix)}
    for path in prefix_roots(prefix):
        sections['root:' + str(path)] = root_digest(path)
    if sdk is not None:
        sections['root:' + str(sdk)] = root_digest(sdk)
        store = trust_store(root)
        sections['trust-store'] = None
        sections['trust-store-key'] = None
        if store is not None:
            # dc-trustcache verifies the store with the public key beside it
            # (<store>.pub) and only a signing pass recreates a removed key.
            key = Path(store).with_suffix(Path(store).suffix + '.pub')
            for name, path in (('trust-store', store), ('trust-store-key', key)):
                try:
                    sections[name] = '%s %s' % (path, sha256(path))
                except OSError:
                    pass
    return sections


def default_root():
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if ((candidate / 'scripts/update.sh').is_file()
                and (candidate / SIGNER).is_file()):
            return candidate
    return here.parent


def default_prefix():
    return os.environ.get('DPREFIX') or str(Path.home() / '.darling')


def record(state, sections):
    unknown = sorted(name for name, value in sections.items() if value is None)
    if unknown:
        fail('runtime signing stamp: cannot record %s; the next update signs again'
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
        note('runtime signing stamp: no usable state at %s' % state)
        return 1
    if not isinstance(recorded, dict) or recorded.get('version') != VERSION:
        note('runtime signing stamp: stale format at %s' % state)
        return 1
    previous = recorded.get('sections')
    if not isinstance(previous, dict):
        note('runtime signing stamp: missing sections at %s' % state)
        return 1
    changed = []
    for name in sorted(set(previous) | set(sections)):
        if name not in previous:
            changed.append(name + ' (new root)')
        elif name not in sections:
            changed.append(name + ' (root gone)')
        elif sections[name] is None:
            changed.append(name + ' (unreadable)')
        elif previous[name] != sections[name]:
            changed.append(name)
    if changed:
        note('runtime signing stamp: changed %s' % ', '.join(changed))
        return 1
    ok('runtime signing stamp: up to date')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('check', 'record'))
    parser.add_argument('--root', default=None,
                        help='repository root (default: derived from this file)')
    parser.add_argument('--prefix', default=None,
                        help='per-user prefix (default: DPREFIX or ~/.darling)')
    parser.add_argument('--sdk', action='store_true',
                        help='also cover the SDK, as a combined --sdk --prefix pass does')
    parser.add_argument('--sdk-dir', default=DEFAULT_SDK,
                        help='SDK covered by --sdk (default: %s)' % DEFAULT_SDK)
    parser.add_argument('--state', default=None,
                        help='stamp file (default: build/runtime-sign-stamp.json)')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    prefix = args.prefix or default_prefix()
    state = Path(args.state) if args.state else root / 'build/runtime-sign-stamp.json'
    sections = collect(root, prefix, args.sdk_dir if args.sdk else None)
    if args.command == 'record':
        return record(state, sections)
    return check(state, sections)


if __name__ == '__main__':
    sys.exit(main())
