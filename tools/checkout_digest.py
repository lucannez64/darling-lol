#!/usr/bin/env python3
"""Digest of the prepared, patched Darling checkout.

The stamps that skip work in an update share this input: the overlay batch, the
explicit CMake configure and the base install all have to rerun when the
checkout under `build/darling-src` changes. HEAD covers a pin change, the
working-tree status covers the files the patches modify and the ones they add,
and every dirty submodule is digested in turn: a patched submodule only shows
as a dirty marker in its parent, which would otherwise hide a patch-added file
inside it (the indium submodule is patched and read by a file(GLOB_RECURSE)).

Everything here fails closed: an unavailable git, a missing checkout or an
unreadable changed file returns None, and every caller treats None as a change.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_digest import sha256  # noqa: E402


def entry(relative, path):
    """One digest line for a file, a symlink or a missing path."""
    if path.is_symlink():
        return '%s\0link\0%s\n' % (relative, os.readlink(path))
    if path.is_file():
        return '%s\0file\0%s\n' % (relative, sha256(path))
    return '%s\0absent\n' % relative


def _git(repo, *arguments):
    return subprocess.run(['git', '-C', str(repo), *arguments],
                          capture_output=True, check=True).stdout


def _is_submodule(path):
    return not path.is_symlink() and path.is_dir() and (path / '.git').exists()


def _tree_digest(repo, prefix=''):
    """HEAD plus every changed path of one repository, submodules included."""
    head = _git(repo, 'rev-parse', 'HEAD').strip().decode('ascii', 'replace')
    if not head:
        return None
    status = _git(repo, 'status', '--porcelain', '-z', '--untracked-files=all',
                  '--no-renames', '--ignore-submodules=none')
    lines = []
    for record in status.split(b'\0'):
        if len(record) < 4:
            continue
        code = record[:2].decode('ascii', 'replace')
        relative = prefix + os.fsdecode(record[3:])
        path = repo / os.fsdecode(record[3:])
        if _is_submodule(path):
            nested = _tree_digest(path, relative + '/')
            if nested is None:
                return None
            lines.append('%s\0%s\0submodule\0%s\n' % (code, relative, nested))
        else:
            lines.append('%s\0%s' % (code, entry(relative, path)))
    hasher = hashlib.sha256(head.encode() + b'\0')
    for line in sorted(lines):
        hasher.update(line.encode())
    return hasher.hexdigest()


def digest(root):
    """Digest of HEAD, the changed files and the dirty submodules."""
    try:
        return _tree_digest(Path(root) / 'build/darling-src')
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
