#!/usr/bin/env python3
"""Skip `cmake --build` when the prepared tree, build script, cache and outputs are unchanged.

`scripts/build.sh` runs `cmake --build build/darling-src/build` on every update.
Even when nothing changed, GNU Make walks hundreds of generated Makefiles.
This stamp records the prepared checkout, `scripts/build.sh`, `CMakeCache.txt`,
compiler versions, stat metadata of built outputs and of the host files the
make dependency graph names after a successful build so a no-op update skips
the build traversal.

The host section exists because make also tracks files outside the checkout:
darlingserver and mldr link /usr/lib/libxml2.so, and the compiler depfiles name
/usr/include headers. A host library or header update used to relink or rebuild
the targets that depend on it; the stamp has to see the same change.

Anything missing, corrupt or unreadable fails closed (returns 1 / rebuilds).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checkout_digest import digest as checkout_digest  # noqa: E402
from console import fail, note, ok  # noqa: E402
from file_digest import sha256  # noqa: E402

VERSION = 1

SCRIPT = 'scripts/build.sh'
BUILD = 'build/darling-src/build'
CACHE = 'build/darling-src/build/CMakeCache.txt'
SKIP_DIRS = ('CMakeFiles',)
# `cmake --install` rewrites these at the top of the build tree; they are not
# build outputs (darling-install-stamp.py tracks them for the install step).
SKIP_TOP_FILES = re.compile(r'install_manifest.*\.txt\Z')
# Make dependency files: per-target rules and link inputs, the merged compiler
# depfile and, under CMakeFiles, the per-object depfiles that CMake merges into
# compiler_depend.make only on the next make run.
DEPFILES = ('build.make', 'compiler_depend.make', 'depend.make')
# A dependency is the first absolute path on a rule line ("target: dep") or on a
# continuation line ("  dep \"); recipe lines start with a tab and run tools
# such as /bin/sh or cmake, which are not dependencies.
DEP_PATH = re.compile(rb'(?m)^(?:[^\t#\s][^\n]*?: +| +)(/[^\s\\]+)')
# Parsing every depfile takes seconds, so the host path list is cached under a
# digest of the depfiles' stat metadata; any depfile change parses again.
HOST_CACHE = 'build/build-stamp-hostdeps.json'
COMPILERS = (
    ('clang', ('clang', '--version')),
    ('cc', ('cc', '--version')),
)


def script_digest(root):
    try:
        return sha256(Path(root) / SCRIPT)
    except OSError:
        return None


def cache_digest(root):
    try:
        return sha256(Path(root) / CACHE)
    except OSError:
        return None


def compiler_digest():
    """Fingerprint available compiler versions; never returns None when absent."""
    values = []
    for name, command in COMPILERS:
        try:
            result = subprocess.run(command, capture_output=True, text=True)
            first = (result.stdout or '').strip().splitlines()
            value = '%d:%s' % (result.returncode, first[0] if first else '')
        except OSError as error:
            value = 'unavailable:%s' % error.errno
        values.append('%s\0%s' % (name, value))
    return hashlib.sha256('\0'.join(values).encode()).hexdigest()


def _walk_outputs(folder, relative, hasher):
    with os.scandir(folder) as scan:
        entries = sorted(scan, key=lambda item: item.name)
    for item in entries:
        path = relative + item.name
        if item.is_symlink():
            hasher.update(('%s\0link\0%s\n' % (path, os.readlink(item.path))).encode())
        elif item.is_dir(follow_symlinks=False):
            if item.name in SKIP_DIRS:
                continue
            _walk_outputs(item.path, path + '/', hasher)
        elif item.is_file(follow_symlinks=False):
            if not relative and SKIP_TOP_FILES.match(item.name):
                continue
            info = item.stat(follow_symlinks=False)
            hasher.update(('%s\0file\0%d\0%d\0%d\n' % (
                path, info.st_size, info.st_mtime_ns, info.st_mode)).encode())


def outputs_digest(root):
    """Stat digest of built outputs outside CMakeFiles; None when unreadable."""
    base = Path(root) / BUILD
    if not base.is_dir():
        return None
    hasher = hashlib.sha256()
    try:
        _walk_outputs(str(base), '', hasher)
    except OSError:
        return None
    return hasher.hexdigest()


def _reraise(error):
    raise error


def _depfiles(base):
    """Sorted (relative path, size, mtime_ns) of every make dependency file."""
    found = []
    for top, dirs, files in os.walk(base, onerror=_reraise):
        dirs.sort()
        relative = os.path.relpath(top, base)
        in_cmakefiles = 'CMakeFiles' in relative.split(os.sep)
        for name in sorted(files):
            if name in DEPFILES or (in_cmakefiles and name.endswith('.d')):
                path = os.path.join(top, name)
                info = os.stat(path)
                found.append((os.path.relpath(path, base), info.st_size, info.st_mtime_ns))
    return found


def _listing_digest(depfiles):
    hasher = hashlib.sha256()
    for relative, size, mtime in depfiles:
        hasher.update(os.fsencode(relative) + b'\0%d\0%d\n' % (size, mtime))
    return hasher.hexdigest()


def _parse_host_paths(base, depfiles, source):
    """Absolute dependency paths outside the prepared checkout, as bytes.

    The checkout and build tree are covered by the tree and outputs sections;
    CMake writes them by their physical path, so both spellings are excluded.
    """
    inside = {os.fsencode(source), os.fsencode(os.path.realpath(source))}
    found = set()
    for relative, _, _ in depfiles:
        with open(os.path.join(base, relative), 'rb') as handle:
            found.update(DEP_PATH.findall(handle.read()))
    return sorted(path for path in found
                  if not any(path == folder or path.startswith(folder + b'/')
                             for folder in inside))


def _load_host_cache(path, listing):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('version') != VERSION \
            or data.get('depfiles') != listing:
        return None
    paths = data.get('paths')
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        return None
    # latin-1 round-trips every byte of a filesystem path.
    return [item.encode('latin-1') for item in paths]


def _store_host_cache(path, listing, paths):
    path = Path(path)
    part = path.with_name(path.name + '.part')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part.write_text(json.dumps({
            'version': VERSION, 'depfiles': listing,
            'paths': [item.decode('latin-1') for item in paths]}) + '\n')
        os.replace(part, path)
    except OSError:
        try:
            part.unlink()
        except OSError:
            pass


def host_digest(root):
    """Stat digest of the host files make tracks; None when unreadable.

    A missing host file is recorded as missing, so a removed library or header
    changes the digest instead of failing the stamp.
    """
    root = Path(root)
    base = root / BUILD
    if not base.is_dir():
        return None
    try:
        depfiles = _depfiles(str(base))
    except OSError:
        return None
    listing = _listing_digest(depfiles)
    cache = root / HOST_CACHE
    paths = _load_host_cache(cache, listing)
    if paths is None:
        try:
            paths = _parse_host_paths(str(base), depfiles, str(base.parent))
        except OSError:
            return None
        _store_host_cache(cache, listing, paths)
    hasher = hashlib.sha256(listing.encode() + b'\0')
    for path in paths:
        try:
            info = os.stat(path)
            target = os.path.realpath(path)
        except OSError:
            hasher.update(path + b'\0missing\n')
            continue
        hasher.update(path + b'\0' + target + b'\0%d\0%d\n' % (info.st_size, info.st_mtime_ns))
    return hasher.hexdigest()


def collect(root):
    return {
        'tree': checkout_digest(root),
        'script': script_digest(root),
        'cache': cache_digest(root),
        'compiler': compiler_digest(),
        'outputs': outputs_digest(root),
        'host': host_digest(root),
    }


def default_root():
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if ((candidate / 'scripts/build.sh').is_file()
                and (candidate / 'tools/checkout_digest.py').is_file()):
            return candidate
    return here.parent


def record(state, sections):
    unknown = sorted(name for name, value in sections.items() if value is None)
    if unknown:
        fail('build stamp: cannot record %s; the next build runs cmake --build'
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
        note('build stamp: no usable state at %s' % state)
        return 1
    if not isinstance(recorded, dict) or recorded.get('version') != VERSION:
        note('build stamp: stale format at %s' % state)
        return 1
    previous = recorded.get('sections')
    if not isinstance(previous, dict):
        note('build stamp: missing sections at %s' % state)
        return 1
    changed = []
    for name in sorted(sections):
        if sections[name] is None:
            changed.append(name + ' (unreadable)')
        elif previous.get(name) != sections[name]:
            changed.append(name)
    if changed:
        note('build stamp: changed %s' % ', '.join(changed))
        return 1
    ok('build stamp: up to date')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('check', 'record'))
    parser.add_argument('--root', default=None,
                        help='repository root (default: derived from this file)')
    parser.add_argument('--state', default=None,
                        help='stamp file (default: build/build-stamp.json)')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    state = Path(args.state) if args.state else root / 'build/build-stamp.json'
    sections = collect(root)
    if args.command == 'record':
        return record(state, sections)
    return check(state, sections)


if __name__ == '__main__':
    sys.exit(main())
