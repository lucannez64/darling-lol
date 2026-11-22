#!/usr/bin/env python3
"""Mirror per-user RuntimeLibraries/RuntimeFrameworks to canonical prefix paths.

Darling mounts the user's prefix as an overlayfs upperdir over the base SDK, so
files under $DPREFIX/usr/lib/... and $DPREFIX/System/Library/Frameworks/...
shadow the SDK at standard macOS paths without needing DYLD_LIBRARY_PATH or
DYLD_FRAMEWORK_PATH in the guest process environment (issue #31).
"""
import argparse
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

DARLING_PRIVATE_LIBS = {'libindium.dylib', 'libiridium.dylib'}
CORESERVICES_SUBFRAMEWORKS = {'CarbonCore.framework', 'LaunchServices.framework'}
BACKUP_MARKERS = ('.before-', '.staged.', '.llvm', '.rt-')


def is_backup_name(name):
    return any(marker in name for marker in BACKUP_MARKERS)


def canonical_library_relpath(name):
    if name == 'libGL.dylib':
        return Path('System/Library/Frameworks/OpenGL.framework/Versions/A/Libraries/libGL.dylib')
    if name.startswith('libsystem_'):
        return Path('usr/lib/system') / name
    if name in DARLING_PRIVATE_LIBS:
        return Path('usr/lib/darling') / name
    return Path('usr/lib') / name


def canonical_framework_relpath(name):
    if name in CORESERVICES_SUBFRAMEWORKS:
        return Path('System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks') / name
    return Path('System/Library/Frameworks') / name


def ensure_inside_prefix(prefix_real, target):
    parent_real = Path(os.path.realpath(target.parent))
    try:
        parent_real.relative_to(prefix_real)
    except ValueError as exc:
        raise ValueError(f'refusing to write outside prefix: {target}') from exc


def files_equal(src, dst):
    try:
        st_src = src.stat()
        st_dst = dst.stat()
    except OSError:
        return False
    if st_src.st_size != st_dst.st_size:
        return False
    with src.open('rb') as a, dst.open('rb') as b:
        while True:
            chunk_a = a.read(65536)
            chunk_b = b.read(65536)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def sync_file(prefix_real, src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    ensure_inside_prefix(prefix_real, dst)
    if dst.is_symlink():
        dst.unlink()
    elif dst.is_file() and files_equal(src, dst):
        return False
    fd, tmp_name = tempfile.mkstemp(prefix='.sync-overlay.', dir=str(dst.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as out_fh, src.open('rb') as in_fh:
            shutil.copyfileobj(in_fh, out_fh)
        mode = stat.S_IMODE(src.stat().st_mode)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dst)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return True


def sync_symlink(prefix_real, src, dst):
    link_target = os.readlink(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ensure_inside_prefix(prefix_real, dst)
    if dst.is_symlink() and os.readlink(dst) == link_target:
        return False
    if dst.is_dir() and not dst.is_symlink():
        shutil.rmtree(dst)
    elif dst.exists() or dst.is_symlink():
        dst.unlink()
    os.symlink(link_target, dst)
    return True


def sync_tree(prefix_real, src_root, dst_root):
    updated = 0
    for top, dirs, files in os.walk(src_root):
        rel_top = Path(top).relative_to(src_root)
        cur_dst_dir = dst_root / rel_top
        cur_dst_dir.mkdir(parents=True, exist_ok=True)
        ensure_inside_prefix(prefix_real, cur_dst_dir / '.probe')

        kept_dirs = []
        for dirname in sorted(dirs):
            if is_backup_name(dirname):
                continue
            src_sub = Path(top) / dirname
            dst_sub = cur_dst_dir / dirname
            if src_sub.is_symlink():
                if sync_symlink(prefix_real, src_sub, dst_sub):
                    updated += 1
            else:
                kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for filename in sorted(files):
            if is_backup_name(filename):
                continue
            src_file = Path(top) / filename
            dst_file = cur_dst_dir / filename
            if src_file.is_symlink():
                if sync_symlink(prefix_real, src_file, dst_file):
                    updated += 1
            elif src_file.is_file():
                if sync_file(prefix_real, src_file, dst_file):
                    updated += 1
    return updated


def sync_prefix(prefix_dir, guest_home):
    prefix = Path(prefix_dir)
    prefix_real = Path(os.path.realpath(prefix))
    guest = Path(guest_home)
    updated = 0

    libs_dir = guest / 'RuntimeLibraries'
    if libs_dir.is_dir():
        for entry in sorted(libs_dir.iterdir()):
            name = entry.name
            if not name.endswith('.dylib') or is_backup_name(name):
                continue
            if entry.is_symlink() or not entry.is_file():
                continue
            dst = prefix / canonical_library_relpath(name)
            if sync_file(prefix_real, entry, dst):
                updated += 1

    fws_dir = guest / 'RuntimeFrameworks'
    if fws_dir.is_dir():
        for entry in sorted(fws_dir.iterdir()):
            name = entry.name
            if not name.endswith('.framework') or is_backup_name(name):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            dst = prefix / canonical_framework_relpath(name)
            updated += sync_tree(prefix_real, entry, dst)

    return updated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', required=True, help='Darling prefix directory')
    parser.add_argument('--guest-home', required=True, help='Host path to guest home inside prefix')
    args = parser.parse_args(argv)
    sync_prefix(args.prefix, args.guest_home)
    return 0


if __name__ == '__main__':
    sys.exit(main())
