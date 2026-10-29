"""Exercise patch migrations against real Git repositories and worktrees."""
import ast
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('git'), 'Git required')
class PatchUpgradeTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'scripts/prepare.py').read_text())
        definitions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name in ('run', 'applies', 'replacement_changes', 'prepare_patch')]
        self.ns = dict(subprocess=subprocess, hashlib=hashlib, os=os, Path=Path, tempfile=tempfile)
        exec(compile(ast.Module(body=definitions, type_ignores=[]), 'prepare.py', 'exec'), self.ns)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.base = ''.join(f'line {i}\n' for i in range(60))
        self.new = self.base.replace('line 40\n', 'line 40\ncollectionBehavior\n')
        self.old = self.new.replace('line 5\n', 'line 5\ncollectionBehavior\n')
        self.header = self.repo / 'window.h'
        self.header.write_text(self.base)
        (self.repo / 'notes').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.header.write_text(self.old)
        self.old_patch = self.git('diff', 'HEAD', '--', 'window.h')
        self.header.write_text(self.new)
        self.patch = self.git('diff', 'HEAD', '--', 'window.h')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def prepare(self, previous=None):
        self.ns['prepare_patch'](self.repo, self.patch, previous if previous is not None else [self.patch, self.old_patch])

    def test_removes_obsolete_hunk_even_when_reverse_check_passes(self):
        self.header.write_text(self.old.replace('line 59', 'user footer'))
        (self.repo / 'notes').write_text('staged user work\n')
        self.git('add', 'notes')
        index = self.git('write-tree')
        (self.repo / 'untracked').write_text('keep me')
        self.assertTrue(self.ns['applies'](self.repo, self.patch, reverse=True))
        self.prepare()  # Includes the falsely recorded new patch before the historical old one.
        self.assertEqual(self.header.read_text(), self.new.replace('line 59', 'user footer'))
        self.assertEqual(self.git('write-tree'), index)
        self.assertEqual((self.repo / 'untracked').read_text(), 'keep me')
        self.assertEqual((self.repo / 'notes').read_text(), 'staged user work\n')

    def test_current_patch_is_not_rewritten(self):
        before = self.header.stat().st_mtime_ns
        self.prepare()
        self.assertEqual(self.header.read_text(), self.new)
        self.assertEqual(self.header.stat().st_mtime_ns, before)

    def test_obsolete_new_file_is_removed(self):
        extra = self.repo / 'obsolete'
        self.header.write_text(self.old)
        extra.write_text('old patch file\n')
        self.git('add', '-N', 'obsolete')
        old = self.git('diff', 'HEAD')
        self.git('reset', '-q', 'HEAD', '--', 'obsolete')
        self.prepare([self.patch, old])
        self.assertEqual(self.header.read_text(), self.new)
        self.assertFalse(extra.exists())

    def test_conflicting_user_edits_are_untouched(self):
        contents = self.old.replace('collectionBehavior', 'user version')
        self.header.write_text(contents)
        index = self.git('write-tree')
        with self.assertRaises(SystemExit):
            self.prepare()
        self.assertEqual(self.header.read_text(), contents)
        self.assertEqual(self.git('write-tree'), index)

    def test_failed_apply_restores_old_patch(self):
        self.header.write_text(self.old)
        original_run = self.ns['run']
        def fail_new(args, **kwargs):
            if 'apply' in args and '--cached' not in args and kwargs.get('input') == self.patch:
                raise subprocess.CalledProcessError(1, args)
            return original_run(args, **kwargs)
        self.ns['run'] = fail_new
        with self.assertRaises(subprocess.CalledProcessError):
            self.prepare()
        self.assertEqual(self.header.read_text(), self.old)
