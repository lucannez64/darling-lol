#!/usr/bin/env python3
"""Export and install a prebuilt per-user Darling runtime overlay (issue #13).

The build produces patched libraries and frameworks under the guest home:

    <guest-home>/RuntimeLibraries
    <guest-home>/RuntimeFrameworks

Compiling them needs the full toolchain, but installing them does not. This tool
packs exactly those two trees into a deterministic archive with a hashed
manifest, and installs that archive into another prefix, or into a staging
directory for a distribution package. It never touches Riot's game files, the
Darling base runtime under /usr/local, or the prefix identity.

    runtime-package.py export  --prefix ~/.darling --output runtime.tar.gz
    runtime-package.py inspect --package runtime.tar.gz
    runtime-package.py install --package runtime.tar.gz --prefix ~/.darling
    runtime-package.py install --package runtime.tar.gz --destdir ./stage

The archive is untrusted input: paths must stay inside the two runtime roots,
hashes are verified before anything is written, and no entry may follow a
symlink out of the tree.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import tempfile
import time

ROOTS = ('RuntimeLibraries', 'RuntimeFrameworks')
MANIFEST = 'runtime-package.json'
SCHEMA = 1
CHUNK = 1024 * 1024


class PackageError(Exception):
    pass


def guest_home_for(prefix, requested=None):
    """Resolve the guest home that holds the runtime overlays."""
    if requested:
        home = Path(requested).expanduser()
    else:
        users = Path(prefix).expanduser() / 'Users'
        candidates = sorted(path for path in users.iterdir()
                            if path.is_dir()) if users.is_dir() else []
        # Riot installs into Users/Shared, so several user directories are normal;
        # only the one that holds a runtime overlay is the guest home.
        with_overlay = [path for path in candidates
                        if (path / ROOTS[0]).is_dir() or (path / ROOTS[1]).is_dir()]
        if len(with_overlay) > 1:
            raise PackageError(
                'several guest homes under %s hold a runtime; pass --guest-home'
                % users)
        if not with_overlay:
            raise PackageError(
                'cannot infer the guest home under %s; pass --guest-home' % users)
        home = with_overlay[0]
    if not home.is_dir():
        raise PackageError('guest home does not exist: %s' % home)
    return home


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def collect(home):
    """Files, symlinks and directories below the two runtime roots."""
    files, links, directories = [], [], []
    for root in ROOTS:
        base = home / root
        if not base.is_dir():
            raise PackageError('missing runtime root: %s' % base)
        for current, names, filenames in os.walk(base, followlinks=False):
            names.sort()
            relative = Path(current).relative_to(home)
            directories.append(relative.as_posix())
            for name in sorted(names):
                path = Path(current) / name
                if path.is_symlink():
                    links.append(path)
                elif path.is_dir():
                    continue
                else:
                    raise PackageError('unsupported entry: %s' % path)
            for name in sorted(filenames):
                path = Path(current) / name
                if path.is_symlink():
                    links.append(path)
                elif path.is_file():
                    files.append(path)
                else:
                    raise PackageError('unsupported entry: %s' % path)
    return directories, files, links


def build_manifest(home, files, links, label):
    return {
        'schema': SCHEMA,
        'label': label,
        'roots': list(ROOTS),
        'files': [{
            'path': path.relative_to(home).as_posix(),
            'size': path.stat().st_size,
            'sha256': hash_file(path),
            'mode': stat.S_IMODE(path.stat().st_mode),
        } for path in sorted(files)],
        'symlinks': [{
            'path': path.relative_to(home).as_posix(),
            'target': os.readlink(path),
        } for path in sorted(links)],
    }


def safe_relative(name):
    """A package path that stays inside a runtime root."""
    if not name or name.startswith('/') or '\\' in name:
        raise PackageError('unsafe package path: %r' % name)
    parts = PurePosixPath(name).parts
    if any(part in ('..', '.') for part in parts):
        raise PackageError('unsafe package path: %r' % name)
    if not parts or parts[0] not in ROOTS:
        raise PackageError('path outside the runtime roots: %r' % name)
    return parts


def check_link(name, target):
    """A relative symlink that cannot resolve outside its runtime root."""
    if not target or target.startswith('/'):
        raise PackageError('absolute symlink in %s: %r' % (name, target))
    root = safe_relative(name)[0]
    resolved = PurePosixPath(*safe_relative(name)[:-1], target)
    stack = []
    for part in resolved.parts:
        if part == '..':
            if not stack:
                raise PackageError('symlink escapes %s: %s -> %s' % (root, name, target))
            stack.pop()
        elif part != '.':
            stack.append(part)
    if not stack or stack[0] != root:
        raise PackageError('symlink escapes %s: %s -> %s' % (root, name, target))
    return target


def add_bytes(archive, name, data, mode=0o644, mtime=0):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    archive.addfile(info, io.BytesIO(data))


def export(home, output, label):
    directories, files, links = collect(home)
    manifest = build_manifest(home, files, links, label)
    data = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    stamp = int(os.environ.get('SOURCE_DATE_EPOCH', '0') or 0)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, 'wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=stamp) as gz:
            with tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as archive:
                add_bytes(archive, MANIFEST, data, mtime=stamp)
                for directory in sorted(directories):
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = stamp
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    archive.addfile(info)
                for entry in manifest['symlinks']:
                    info = tarfile.TarInfo(entry['path'])
                    info.type = tarfile.SYMTYPE
                    info.linkname = entry['target']
                    info.mtime = stamp
                    info.uid = info.gid = 0
                    archive.addfile(info)
                for entry in manifest['files']:
                    info = tarfile.TarInfo(entry['path'])
                    info.size = entry['size']
                    info.mode = entry['mode']
                    info.mtime = stamp
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    with open(home / entry['path'], 'rb') as source:
                        archive.addfile(info, source)
    return manifest


def read_package(package):
    """Open an archive and its manifest; the caller closes the archive."""
    package = Path(package)
    try:
        archive = tarfile.open(package, 'r:gz')
    except (OSError, tarfile.TarError) as error:
        raise PackageError('cannot read %s: %s' % (package, error))
    try:
        try:
            member = archive.getmember(MANIFEST)
        except KeyError:
            raise PackageError('no %s in %s' % (MANIFEST, package))
        manifest = json.loads(archive.extractfile(member).read().decode())
        members = {item.name: item for item in archive.getmembers()
                   if item.name != MANIFEST}
        return manifest, members, archive
    except Exception:
        archive.close()
        raise


def validate(manifest, members):
    if manifest.get('schema') != SCHEMA:
        raise PackageError('unsupported package schema: %r' % manifest.get('schema'))
    expected = {}
    for entry in manifest.get('files', []):
        parts = safe_relative(entry['path'])
        expected['/'.join(parts)] = ('file', entry)
    for entry in manifest.get('symlinks', []):
        parts = safe_relative(entry['path'])
        check_link('/'.join(parts), entry['target'])
        expected['/'.join(parts)] = ('link', entry)
    for name, member in members.items():
        parts = safe_relative(name)
        key = '/'.join(parts)
        if member.isdir():
            continue
        if key not in expected:
            raise PackageError('unexpected archive member: %s' % name)
        kind, entry = expected[key]
        if kind == 'file' and not member.isreg():
            raise PackageError('not a regular file: %s' % name)
        if kind == 'link' and not member.issym():
            raise PackageError('not a symlink: %s' % name)
    missing = sorted(set(expected) - set(members))
    if missing:
        raise PackageError('missing archive members: %s' % ', '.join(missing[:5]))


def destination(root, name):
    parts = safe_relative(name)
    target = Path(root).joinpath(*parts)
    for ancestor in target.parents:
        if ancestor == Path(root):
            break
        if ancestor.is_symlink():
            raise PackageError('refusing symlinked destination: %s' % ancestor)
    return target


def install(package, root, dry_run=False, out=print):
    manifest, members, archive = read_package(package)
    validate(manifest, members)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.runtime-package-', dir=root))
    installed = removed = 0
    try:
        for entry in manifest['files']:
            target = destination(root, entry['path'])
            member = members[entry['path']]
            staged = staging / hashlib.sha256(entry['path'].encode()).hexdigest()
            digest = hashlib.sha256()
            with archive.extractfile(member) as source, open(staged, 'wb') as sink:
                while True:
                    block = source.read(CHUNK)
                    if not block:
                        break
                    digest.update(block)
                    sink.write(block)
            if digest.hexdigest() != entry['sha256']:
                raise PackageError('checksum mismatch: %s' % entry['path'])
            if dry_run:
                out('would install %s' % entry['path'])
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                if target.is_dir() and not target.is_symlink():
                    raise PackageError('refusing to replace directory: %s' % target)
                target.unlink()
                removed += 1
            os.chmod(staged, entry['mode'])
            os.replace(staged, target)
            installed += 1
        for entry in manifest['symlinks']:
            target = destination(root, entry['path'])
            link = check_link(entry['path'], entry['target'])
            if dry_run:
                out('would link %s -> %s' % (entry['path'], link))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
                removed += 1
            os.symlink(link, target)
            installed += 1
    finally:
        archive.close()
        shutil.rmtree(staging, ignore_errors=True)
    out('installed %d entries (%d replaced) into %s' % (installed, removed, root))
    return manifest


def describe(package, out=print):
    manifest, members, archive = read_package(package)
    try:
        validate(manifest, members)
    finally:
        archive.close()
    total = sum(entry['size'] for entry in manifest['files'])
    out('label: %s' % (manifest.get('label') or '(none)'))
    out('schema: %d' % manifest['schema'])
    out('files: %d, symlinks: %d, bytes: %d' %
        (len(manifest['files']), len(manifest['symlinks']), total))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)

    export_parser = commands.add_parser('export', help='pack a prefix runtime into an archive')
    export_parser.add_argument('--prefix', type=Path, required=True)
    export_parser.add_argument('--guest-home', type=Path)
    export_parser.add_argument('--output', type=Path, required=True)
    export_parser.add_argument('--label', default='')

    inspect_parser = commands.add_parser('inspect', help='show an archive without installing it')
    inspect_parser.add_argument('--package', type=Path, required=True)

    install_parser = commands.add_parser('install', help='install an archive into a prefix')
    install_parser.add_argument('--package', type=Path, required=True)
    install_parser.add_argument('--prefix', type=Path)
    install_parser.add_argument('--guest-home', type=Path)
    install_parser.add_argument('--destdir', type=Path,
                                help='write below this directory instead of the guest home')
    install_parser.add_argument('--dry-run', action='store_true')

    args = parser.parse_args(argv)
    try:
        if args.command == 'export':
            home = guest_home_for(args.prefix, args.guest_home)
            manifest = export(home, args.output, args.label)
            print('wrote %s (%d files, %d symlinks)' %
                  (args.output, len(manifest['files']), len(manifest['symlinks'])))
        elif args.command == 'inspect':
            describe(args.package)
        else:
            if args.destdir:
                root = args.destdir
            else:
                if not args.prefix:
                    parser.error('install needs --prefix or --destdir')
                root = guest_home_for(args.prefix, args.guest_home)
            install(args.package, root, dry_run=args.dry_run)
    except PackageError as error:
        print('runtime-package: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
