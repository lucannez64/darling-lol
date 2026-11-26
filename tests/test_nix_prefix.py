"""darling-lol --set-prefix on temporary directories; no Darling or namespace."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class PrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.workspace = self.folder / 'workspace'
        (self.workspace / 'scripts').mkdir(parents=True)
        (self.workspace / 'tools').mkdir()
        for name in ('nix-prefix.py', 'configure.py', 'common.py'):
            shutil.copy(ROOT / 'scripts' / name, self.workspace / 'scripts' / name)
        self.old = self.folder / 'home/.darling'
        (self.old / 'Volumes/SystemRoot').mkdir(parents=True)
        (self.old / 'Applications/League of Legends.app').mkdir(parents=True)
        (self.old / 'Applications/League of Legends.app/game').write_text('game data')
        (self.folder / 'home/.darling.workdir/work').mkdir(parents=True)

    def set_prefix(self, new):
        return subprocess.run([sys.executable, str(self.workspace / 'scripts/nix-prefix.py'), str(new)],
                              env=dict(os.environ, DPREFIX=str(self.old), HOME=str(self.folder / 'home'),
                                       PYTHONDONTWRITEBYTECODE='1'),
                              text=True, capture_output=True, timeout=30)

    def require_user_xattrs(self):
        # A real cross-device copy checks user.* xattrs on the target; some
        # build sandboxes (e.g. Nix's /build) do not support them.
        probe = self.folder / '.xattr-probe'
        probe.touch()
        try:
            os.setxattr(probe, 'user.test', b'1')
        except OSError:
            self.skipTest('the temporary directory has no user xattrs')
        finally:
            probe.unlink()

    def saved_prefix(self):
        return json.loads((self.workspace / 'build/config.json').read_text())['prefix']

    def test_moves_the_prefix_and_saves_it(self):
        new = self.folder / 'bulk/Games/League Of Legends'
        result = self.set_prefix(new)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((new / 'Applications/League of Legends.app/game').read_text(), 'game data')
        self.assertFalse(self.old.exists())
        self.assertFalse((self.folder / 'home/.darling.workdir').exists())
        self.assertEqual(self.saved_prefix(), str(new))

    def test_copies_across_filesystems_and_removes_the_old_copy_last(self):
        self.require_user_xattrs()
        spec = importlib.util.spec_from_file_location('nix_prefix', self.workspace / 'scripts/nix-prefix.py')
        prefix = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prefix)
        rename = os.rename
        calls = []

        def cross_device(source, target):
            calls.append(Path(target).name)
            if len(calls) == 1:
                raise OSError(errno.EXDEV, 'cross-device link')
            self.assertTrue(self.old.exists(), 'old prefix removed before the copy finished')
            rename(source, target)

        new = self.folder / 'bulk/prefix'
        with patch.object(prefix.os, 'rename', cross_device):
            leftover = prefix.move(self.old, new)
        # A failed rename of the prefix, then the rename of the finished copy.
        self.assertEqual(calls, ['prefix', 'prefix'])
        self.assertEqual((new / 'Applications/League of Legends.app/game').read_text(), 'game data')
        # The caller removes the old prefix once the new path is saved.
        self.assertEqual(leftover, self.old)
        self.assertTrue(self.old.exists())
        self.assertEqual(list((self.folder / 'bulk').iterdir()), [new])

    def load(self):
        spec = importlib.util.spec_from_file_location('nix_prefix_unsafe', self.workspace / 'scripts/nix-prefix.py')
        prefix = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prefix)
        return prefix

    def cross_device(self, prefix):
        rename = os.rename
        first = []

        def rename_across(source, target):
            if not first:
                first.append(1)
                raise OSError(errno.EXDEV, 'cross-device link')
            rename(source, target)
        return patch.object(prefix.os, 'rename', rename_across)

    def test_copy_leaves_an_existing_partial_directory_alone(self):
        self.require_user_xattrs()
        prefix = self.load()
        new = self.folder / 'bulk/prefix'
        unrelated = self.folder / 'bulk/prefix.partial'
        unrelated.mkdir(parents=True)
        (unrelated / 'keep').write_text('mine')
        with self.cross_device(prefix):
            prefix.move(self.old, new)
        self.assertEqual((unrelated / 'keep').read_text(), 'mine')
        self.assertEqual(sorted(path.name for path in (self.folder / 'bulk').iterdir()),
                         ['prefix', 'prefix.partial'])

    def test_failed_copy_keeps_the_old_prefix_and_removes_only_its_staging(self):
        prefix = self.load()
        new = self.folder / 'bulk/prefix'
        new.parent.mkdir()
        error = subprocess.CalledProcessError(1, 'cp')
        with self.cross_device(prefix), patch.object(prefix, 'check_xattrs'), \
                patch.object(prefix.subprocess, 'run', side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                prefix.move(self.old, new)
        self.assertEqual((self.old / 'Applications/League of Legends.app/game').read_text(), 'game data')
        self.assertEqual(list(new.parent.iterdir()), [])

    def test_copy_refused_when_the_target_drops_user_xattrs(self):
        prefix = self.load()
        new = self.folder / 'bulk/prefix'
        with self.cross_device(prefix), patch.object(prefix.os, 'setxattr',
                                                     side_effect=OSError(errno.ENOTSUP, 'not supported')):
            with self.assertRaises(SystemExit) as raised:
                prefix.move(self.old, new)
        self.assertIn('extended attributes', str(raised.exception))
        self.assertTrue(self.old.exists())
        self.assertEqual(list(new.parent.iterdir()), [])

    def test_copy_that_lost_xattrs_is_refused(self):
        prefix = self.load()
        game = self.old / 'Applications/League of Legends.app/game'
        try:
            os.setxattr(game, 'user.test', b'1')
        except OSError:
            self.skipTest('the temporary directory has no user xattrs')
        new = self.folder / 'bulk/prefix'
        real = prefix.user_xattrs
        stripped = lambda path: {} if Path(path).name == 'game' and Path(path).is_relative_to(new.parent) else real(path)
        with self.cross_device(prefix), patch.object(prefix, 'user_xattrs', stripped):
            with self.assertRaises(SystemExit) as raised:
                prefix.move(self.old, new)
        self.assertIn('old prefix was kept', str(raised.exception))
        self.assertTrue(game.exists())
        self.assertEqual(list(new.parent.iterdir()), [])

    def test_failed_delete_of_the_old_prefix_keeps_the_new_one_saved(self):
        prefix = self.load()
        new = self.folder / 'bulk/prefix'
        with patch.dict(os.environ, {'DPREFIX': str(self.old)}), \
                patch.object(sys, 'argv', ['nix-prefix.py', str(new)]), \
                patch.object(prefix, 'ROOT', self.workspace), \
                patch.object(prefix, 'move', lambda old, target: (shutil.copytree(old, target), old)[1]), \
                patch.object(prefix.shutil, 'rmtree', side_effect=OSError('busy')):
            self.assertEqual(prefix.main(), 0)
        self.assertEqual(self.saved_prefix(), str(new))
        self.assertTrue((new / 'Applications/League of Legends.app/game').exists())
        self.assertTrue(self.old.exists())

    def test_refuses_a_destination_inside_the_overlay_work_directory(self):
        for new in (self.folder / 'home/.darling.workdir/games', self.folder / 'home/.darling.workdir'):
            result = self.set_prefix(new)
            self.assertNotEqual(result.returncode, 0, new)
            self.assertIn('work', result.stderr)
        self.assertTrue((self.old / 'Applications/League of Legends.app/game').exists())
        self.assertFalse((self.workspace / 'build/config.json').exists())

    def test_switches_to_an_existing_prefix_without_moving(self):
        other = self.folder / 'other'
        (other / 'Volumes/SystemRoot').mkdir(parents=True)
        result = self.set_prefix(other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.old / 'Applications/League of Legends.app/game').exists())
        self.assertEqual(self.saved_prefix(), str(other))

    def test_refuses_a_directory_with_other_files(self):
        games = self.folder / 'Games'
        games.mkdir()
        (games / 'other-game').mkdir()
        result = self.set_prefix(games)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a Darling prefix', result.stderr)
        self.assertTrue(self.old.exists())
        self.assertFalse((self.workspace / 'build/config.json').exists())

    def test_refuses_overlay_separators_and_nested_paths(self):
        for new in (self.folder / 'a,b', self.folder / 'a:b', self.old / 'inner'):
            result = self.set_prefix(new)
            self.assertNotEqual(result.returncode, 0, new)
        self.assertTrue(self.old.exists())


if __name__ == '__main__':
    unittest.main()
