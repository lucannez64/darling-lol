#!/usr/bin/env python3
"""Digest of the prepared, patched Darling checkout.

The stamps that skip work in an update share this input: the overlay batch and
the explicit CMake configure both have to rerun when the checkout under
`build/darling-src` changes. HEAD covers a pin change, `git diff HEAD` covers
the patched working tree, and the untracked files cover the ones the patch
stack creates (`new file mode`), which `git diff HEAD` does not show.

Everything here fails closed: an unavailable git, a missing checkout or an
unreadable patch-added file returns None, and every caller treats None as a
change.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_digest import sha256  # noqa: E402


def entry(relative, path):
    """One digest line for a file or a symlink, stable across runs."""
    if path.is_symlink():
        return '%s\0link\0%s\n' % (relative, os.readlink(path))
    return '%s\0file\0%s\n' % (relative, sha256(path))


def digest(root):
    """Digest of HEAD, the working-tree diff and the patch-added files."""
    source = Path(root) / 'build/darling-src'
    try:
        head = subprocess.run(
            ['git', '-C', str(source), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, check=True).stdout.strip()
        diff = subprocess.run(
            ['git', '-C', str(source), 'diff', 'HEAD', '--binary', '--no-color'],
            capture_output=True, check=True).stdout
        status = subprocess.run(
            ['git', '-C', str(source), 'status', '--porcelain', '-z',
             '--untracked-files=all', '--no-renames'],
            capture_output=True, check=True).stdout
        added = []
        for record in status.split(b'\0'):
            if not record.startswith(b'?? '):
                continue
            path = source / os.fsdecode(record[3:])
            added.append(entry(path.relative_to(source).as_posix(), path))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    if not head:
        return None
    extra = ''.join(sorted(added)).encode()
    return hashlib.sha256(head.encode() + b'\0' + diff + b'\0' + extra).hexdigest()
