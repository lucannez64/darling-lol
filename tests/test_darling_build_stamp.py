"""Exercise darling-build-stamp.py and checkout_digest session caching."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import git_environment  # noqa: E402

spec = importlib.util.spec_from_file_location(
    'darling_build_stamp', ROOT / 'tools/darling-build-stamp.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)

digest_spec = importlib.util.spec_from_file_location(
    'checkout_digest', ROOT / 'tools/checkout_digest.py')
checkout_digest_mod = importlib.util.module_from_spec(digest_spec)
digest_spec.loader.exec_module(checkout_digest_mod)


def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), *arguments], check=True,
                   capture_output=True)


def build_fixture(root):
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/build.sh').write_text('#!/bin/sh\ncmake --build src/build\n')
    base = root / 'build/darling-src'
    base.mkdir(parents=True)
    git(base, 'init', '-q')
    (base / '.gitignore').write_text('build/\n')
    (base / 'CMakeLists.txt').write_text('project(demo)\n')
    git(base, 'add', '.gitignore', 'CMakeLists.txt')
    git(base, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', 'fixture')
    build = base / 'build'
    build.mkdir()
    (build / 'CMakeCache.txt').write_text('CMAKE_BUILD_TYPE:STRING=\n')
    (build / 'libdemo.dylib').write_bytes(b'built-dylib')
    # Intermediate objects are excluded from the build output digest.
    (build / 'CMakeFiles/Demo.dir').mkdir(parents=True)
    (build / 'CMakeFiles/Demo.dir/obj.o').write_bytes(b'obj')
    return root


class BuildStampTests(unittest.TestCase):
    def setUp(self):
        git_environment.isolate(self)
        self.folder = tempfile.TemporaryDirectory(prefix='build-stamp-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        build_fixture(self.root)
        self.state = self.root / 'build/build-stamp.json'
        self.env = patch.dict(os.environ, {'RCLOL_CHECKOUT_DIGEST_CACHE': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self):
        return stamp.record(self.state, stamp.collect(self.root))

    def check(self):
        return stamp.check(self.state, stamp.collect(self.root))

    def test_recorded_state_allows_the_fast_path(self):
        self.assertEqual(0, self.record())
        self.assertEqual(0, self.check())
        self.assertEqual(stamp.VERSION, json.loads(self.state.read_text())['version'])

    def test_missing_or_corrupt_state_rebuilds(self):
        self.assertEqual(1, self.check())
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{not json')
        self.assertEqual(1, self.check())
        self.state.write_text(json.dumps({'version': stamp.VERSION - 1, 'sections': {}}))
        self.assertEqual(1, self.check())

    def test_changed_inputs_and_outputs_rebuild(self):
        changes = (
            ('script', self.root / 'scripts/build.sh'),
            ('cache', self.root / 'build/darling-src/build/CMakeCache.txt'),
            ('tree', self.root / 'build/darling-src/CMakeLists.txt'),
            ('outputs', self.root / 'build/darling-src/build/libdemo.dylib'),
        )
        for section, path in changes:
            with self.subTest(section=section):
                self.assertEqual(0, self.record())
                self.assertEqual(0, self.check())
                original = path.read_bytes()
                path.write_bytes(original + b'\nchanged')
                self.assertEqual(1, self.check())
                path.write_bytes(original)

    def test_removed_output_rebuilds(self):
        self.assertEqual(0, self.record())
        (self.root / 'build/darling-src/build/libdemo.dylib').unlink()
        self.assertEqual(1, self.check())

    def test_cmakefiles_changes_are_ignored(self):
        self.assertEqual(0, self.record())
        (self.root / 'build/darling-src/build/CMakeFiles/Demo.dir/obj.o').write_bytes(
            b'updated-obj')
        self.assertEqual(0, self.check())

    def test_missing_cache_or_checkout_cannot_be_recorded(self):
        (self.root / 'build/darling-src/build/CMakeCache.txt').unlink()
        self.assertEqual(1, self.record())
        self.assertFalse(self.state.exists())
        shutil.rmtree(self.root / 'build/darling-src')
        self.assertEqual(1, self.record())

    def test_cli_checks_the_same_state(self):
        for command in ('record', 'check'):
            self.assertEqual(0, subprocess.run(
                [sys.executable, str(ROOT / 'tools/darling-build-stamp.py'),
                 '--root', str(self.root), '--state', str(self.state), command],
                capture_output=True).returncode)


class CheckoutDigestCacheTests(unittest.TestCase):
    def setUp(self):
        git_environment.isolate(self)
        self.folder = tempfile.TemporaryDirectory(prefix='digest-cache-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        build_fixture(self.root)
        self.cache = self.root / 'digest-cache.json'

    def test_session_cache_reuses_digest_without_rerunning_git(self):
        with patch.dict(os.environ, {'RCLOL_CHECKOUT_DIGEST_CACHE': str(self.cache)}):
            first = checkout_digest_mod.digest(self.root)
            self.assertIsNotNone(first)
            self.assertTrue(self.cache.is_file())
            with patch.object(checkout_digest_mod, '_tree_digest',
                              side_effect=AssertionError('should use cache')):
                self.assertEqual(first, checkout_digest_mod.digest(self.root))

    def test_mismatched_root_or_corrupt_cache_recomputes(self):
        self.cache.write_text('{corrupt')
        with patch.dict(os.environ, {'RCLOL_CHECKOUT_DIGEST_CACHE': str(self.cache)}):
            fresh = checkout_digest_mod.digest(self.root)
            self.assertIsNotNone(fresh)
            # A cache entry written for another root is ignored.
            self.cache.write_text(json.dumps({'root': '/other/root', 'digest': '0' * 64}))
            self.assertEqual(fresh, checkout_digest_mod.digest(self.root))


if __name__ == '__main__':
    unittest.main()
