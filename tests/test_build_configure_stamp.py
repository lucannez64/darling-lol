"""Exercise the configure stamp that lets scripts/build.sh skip `cmake -S -B`."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import git_environment  # noqa: E402

spec = importlib.util.spec_from_file_location(
    'build_configure_stamp', ROOT / 'tools/build-configure-stamp.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)


def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), *arguments], check=True,
                   capture_output=True)


def build_fixture(root):
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/build.sh').write_text('#!/bin/sh\ncmake -S src -B build\n')
    base = root / 'build/darling-src'
    base.mkdir(parents=True)
    git(base, 'init', '-q')
    (base / 'CMakeLists.txt').write_text('project(demo)\n')
    git(base, 'add', 'CMakeLists.txt')
    git(base, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', 'fixture')
    cache = base / 'build/CMakeCache.txt'
    cache.parent.mkdir(parents=True)
    cache.write_text('CMAKE_BUILD_TYPE:STRING=\nENABLE_METAL:BOOL=ON\n')
    # A patched submodule, the way the indium patch leaves one behind: the
    # superproject records a gitlink and only reports it as dirty.
    nested = base / 'src/external/demo'
    nested.mkdir(parents=True)
    git(nested, 'init', '-q')
    (nested / 'CMakeLists.txt').write_text('project(nested)\n')
    (nested / 'src.c').write_text('int demo(void) { return 0; }\n')
    git(nested, 'add', 'CMakeLists.txt', 'src.c')
    git(nested, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', 'nested fixture')
    commit = subprocess.run(
        ['git', '-C', str(nested), 'rev-parse', 'HEAD'], check=True,
        capture_output=True, text=True).stdout.strip()
    subprocess.run(
        ['git', '-C', str(base), 'update-index', '--add', '--cacheinfo',
         '160000,%s,src/external/demo' % commit], check=True, capture_output=True)
    git(base, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', 'add the submodule fixture')
    return root


class ConfigureStampTests(unittest.TestCase):
    def setUp(self):
        # A hook can point Git at its own repository; keep the fixture here.
        git_environment.isolate(self)
        self.folder = tempfile.TemporaryDirectory(prefix='configure-stamp-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        build_fixture(self.root)
        self.state = self.root / 'build/configure-stamp.json'

    def record(self):
        return stamp.record(self.state, stamp.collect(self.root))

    def check(self):
        return stamp.check(self.state, stamp.collect(self.root))

    def test_recorded_state_allows_the_fast_path(self):
        self.assertEqual(0, self.record())
        self.assertEqual(0, self.check())
        self.assertEqual(stamp.VERSION, json.loads(self.state.read_text())['version'])

    def test_missing_or_corrupt_state_reconfigures(self):
        self.assertEqual(1, self.check())
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{not json')
        self.assertEqual(1, self.check())
        self.state.write_text(json.dumps({'version': stamp.VERSION - 1, 'sections': {}}))
        self.assertEqual(1, self.check())

    def test_changed_inputs_reconfigure(self):
        changes = (
            ('script', self.root / 'scripts/build.sh'),
            ('cache', self.root / 'build/darling-src/build/CMakeCache.txt'),
            ('tree', self.root / 'build/darling-src/CMakeLists.txt'),
        )
        for section, path in changes:
            with self.subTest(section=section):
                self.assertEqual(0, self.record())
                self.assertEqual(0, self.check())
                original = path.read_bytes()
                path.write_bytes(original + b'\n# changed\n')
                self.assertEqual(1, self.check())
                path.write_bytes(original)

    def test_patch_added_files_reconfigure(self):
        # A patch that adds a file a file(GLOB) would pick up does not touch any
        # CMakeLists.txt, so make's own timestamp check cannot see it.
        self.assertEqual(0, self.record())
        added = self.root / 'build/darling-src/src/added.c'
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_text('int added(void) { return 0; }\n')
        self.assertEqual(1, self.check())

    def test_patch_added_file_in_a_submodule_reconfigures(self):
        # The indium patch adds files inside a submodule; the superproject only
        # reports the submodule as dirty, so the digest has to descend into it.
        self.assertEqual(0, self.record())
        added = self.root / 'build/darling-src/src/external/demo/src/added.c'
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_text('int added(void) { return 0; }\n')
        self.assertEqual(1, self.check())

    def test_changed_file_in_a_submodule_reconfigures(self):
        self.assertEqual(0, self.record())
        changed = self.root / 'build/darling-src/src/external/demo/src.c'
        changed.write_text('int demo(void) { return 1; }\n')
        self.assertEqual(1, self.check())

    def test_missing_cache_cannot_be_recorded(self):
        (self.root / 'build/darling-src/build/CMakeCache.txt').unlink()
        self.assertEqual(1, self.record())
        self.assertFalse(self.state.exists())

    def test_missing_checkout_cannot_be_recorded(self):
        shutil.rmtree(self.root / 'build/darling-src')
        self.assertEqual(1, self.record())

    def test_cli_checks_the_same_state(self):
        for command in ('record', 'check'):
            self.assertEqual(0, subprocess.run(
                [sys.executable, str(ROOT / 'tools/build-configure-stamp.py'),
                 '--root', str(self.root), '--state', str(self.state), command],
                capture_output=True).returncode)


if __name__ == '__main__':
    unittest.main()
