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
        self.host_folder = tempfile.TemporaryDirectory(prefix='build-stamp-host-')
        self.addCleanup(self.host_folder.cleanup)
        self.host = Path(self.host_folder.name)
        self.write_host_dependencies()

    def write_host_dependencies(self):
        """Make files naming host files the way CMake's Unix Makefiles do."""
        (self.host / 'lib').mkdir()
        (self.host / 'include').mkdir()
        (self.host / 'bin').mkdir()
        (self.host / 'lib/libxml2.so.16').write_bytes(b'libxml2')
        (self.host / 'lib/libxml2.so').symlink_to('libxml2.so.16')
        (self.host / 'include/merged.h').write_text('/* merged */\n')
        (self.host / 'include/object.h').write_text('/* object */\n')
        (self.host / 'bin/tool').write_text('#!/bin/sh\n')
        build = self.root / 'build/darling-src/build'
        target = build / 'CMakeFiles/Demo.dir'
        source = self.root / 'build/darling-src/CMakeLists.txt'
        (target / 'build.make').write_text(
            '# CMAKE generated file: DO NOT EDIT!\n'
            'SHELL = /bin/sh\n'
            'libdemo.dylib: %s/lib/libxml2.so\n'
            'libdemo.dylib: CMakeFiles/Demo.dir/obj.o\n'
            '\t%s/bin/tool --link libdemo.dylib\n' % (self.host, self.host))
        (target / 'compiler_depend.make').write_text(
            'CMakeFiles/Demo.dir/obj.o: %s \\\n'
            '  %s/include/merged.h\n' % (source, self.host))
        (target / 'obj.o.d').write_text(
            'CMakeFiles/Demo.dir/obj.o: %s \\\n'
            '  %s/include/object.h\n' % (source, self.host))

    def touch(self, path):
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

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

    def test_install_manifest_changes_are_ignored(self):
        build = self.root / 'build/darling-src/build'
        (build / 'install_manifest.txt').write_text('/usr/local/old\n')
        self.assertEqual(0, self.record())
        # cmake --install rewrites the manifests after every install.
        (build / 'install_manifest.txt').write_text('/usr/local/new\n')
        (build / 'install_manifest_runtime.txt').write_text('/usr/local/runtime\n')
        self.assertEqual(0, self.check())
        # The exclusion is limited to the manifests at the top of the tree.
        (build / 'sub').mkdir()
        (build / 'sub/install_manifest.txt').write_text('output\n')
        self.assertEqual(1, self.check())

    def test_host_dependency_changes_rebuild(self):
        changes = (
            ('linked library', self.host / 'lib/libxml2.so.16'),
            ('merged depfile header', self.host / 'include/merged.h'),
            ('per-object depfile header', self.host / 'include/object.h'),
        )
        for label, path in changes:
            with self.subTest(dependency=label):
                self.assertEqual(0, self.record())
                self.assertEqual(0, self.check())
                self.touch(path)
                self.assertEqual(1, self.check())

    def test_relinked_library_symlink_rebuilds(self):
        self.assertEqual(0, self.record())
        # A soname bump repoints the development symlink at a new library.
        (self.host / 'lib/libxml2.so.17').write_bytes(b'libxml2')
        os.utime(self.host / 'lib/libxml2.so.17',
                 ns=(0, (self.host / 'lib/libxml2.so.16').stat().st_mtime_ns))
        (self.host / 'lib/libxml2.so').unlink()
        (self.host / 'lib/libxml2.so').symlink_to('libxml2.so.17')
        self.assertEqual(1, self.check())

    def test_removed_host_dependency_rebuilds_and_records(self):
        self.assertEqual(0, self.record())
        (self.host / 'include/merged.h').unlink()
        self.assertEqual(1, self.check())
        # A missing host file is part of the state, not an unreadable section.
        self.assertEqual(0, self.record())
        self.assertEqual(0, self.check())

    def test_recipe_tools_and_variables_are_not_dependencies(self):
        self.assertEqual(0, self.record())
        self.touch(self.host / 'bin/tool')
        self.assertEqual(0, self.check())
        paths = stamp._load_host_cache(
            self.root / stamp.HOST_CACHE,
            stamp._listing_digest(stamp._depfiles(str(self.root / stamp.BUILD))))
        self.assertEqual(sorted(os.fsencode(self.host / name) for name in (
            'include/merged.h', 'include/object.h', 'lib/libxml2.so')), paths)

    def test_host_paths_are_cached_until_a_depfile_changes(self):
        self.assertEqual(0, self.record())
        with patch.object(stamp, '_parse_host_paths',
                          side_effect=AssertionError('should use the cache')):
            self.assertEqual(0, self.check())
        depfile = self.root / 'build/darling-src/build/CMakeFiles/Demo.dir/obj.o.d'
        depfile.write_text(depfile.read_text() + '  %s/include/merged.h\n' % self.host)
        self.assertEqual(1, self.check())
        # A corrupt cache parses again instead of trusting it.
        (self.root / stamp.HOST_CACHE).write_text('{corrupt')
        self.assertEqual(0, self.record())
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
