"""Exercise the runtime overlay stamp that lets an update skip the rebuild."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'runtime_overlay_stamp', ROOT / 'tools/runtime-overlay-stamp.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)


def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), *arguments], check=True,
                   capture_output=True)


def build_fixture(root):
    """A repository root with a patched checkout, cmake tree and prefix."""
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/install-runtime.sh').write_text('#!/bin/sh\n')
    tools = root / 'tools'
    tools.mkdir()
    (tools / 'riot_build_cache.py').write_text('# support module\n')
    (tools / 'build-riot-demo.py').write_text('print("demo")\n')
    config = root / 'build/config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'prefix': str(root / 'prefix'),
                                  'guest_home': '/Users/tester'}) + '\n')

    base = root / 'build/darling-src'
    base.mkdir()
    git(base, 'init', '-q')
    (base / 'source.c').write_text('int main(void) { return 0; }\n')
    git(base, 'add', 'source.c')
    git(base, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', 'fixture')

    build = base / 'build'
    for directory in stamp.TARGET_DIRS:
        folder = build / directory / 'CMakeFiles/Demo.dir'
        folder.mkdir(parents=True)
        (folder / 'flags.make').write_text('C_FLAGS = -O2\n')
        (folder / 'link.txt').write_text('clang -o demo\n')
    for artifact in stamp.ARTIFACTS:
        (build / artifact).parent.mkdir(parents=True, exist_ok=True)
        (build / artifact).write_bytes(b'artifact:' + artifact.encode())

    prefix = root / 'prefix'
    guest = prefix / 'Users/tester'
    (guest / 'RuntimeLibraries').mkdir(parents=True)
    (guest / 'RuntimeLibraries/libsystem_c.dylib').write_bytes(b'installed')
    (guest / 'RuntimeFrameworks/AppKit.framework').mkdir(parents=True)
    (guest / 'RuntimeFrameworks/AppKit.framework/AppKit').write_bytes(b'installed')
    (prefix / 'usr/lib').mkdir(parents=True)
    (prefix / 'usr/lib/libGL.dylib').write_bytes(b'installed')
    (prefix / 'System/Library/Frameworks').mkdir(parents=True)
    return root


class OverlayStampTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        build_fixture(self.root)
        self.state = self.root / 'build/runtime/overlay-stamp.json'
        # The installers resolve DPREFIX/RIOT_GUEST_HOME from the environment too.
        self.env = patch.dict(os.environ, {'DPREFIX': '', 'RIOT_GUEST_HOME': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self):
        self.assertEqual(0, stamp.record(self.state, stamp.collect(self.root)))

    def check(self):
        return stamp.check(self.state, stamp.collect(self.root))

    def test_recorded_state_allows_the_fast_path(self):
        self.record()
        self.assertEqual(0, self.check())
        self.assertEqual(stamp.VERSION, json.loads(self.state.read_text())['version'])

    def test_missing_or_corrupt_state_forces_a_rebuild(self):
        self.assertEqual(1, self.check())
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{not json')
        self.assertEqual(1, self.check())
        self.state.write_text(json.dumps({'version': stamp.VERSION - 1, 'sections': {}}))
        self.assertEqual(1, self.check())

    def test_changed_overlay_inputs_force_a_rebuild(self):
        changes = (
            ('tools', self.root / 'tools/build-riot-demo.py'),
            ('flags', self.root / 'build/darling-src/build/src/external/libplatform/'
                                  'CMakeFiles/Demo.dir/flags.make'),
            ('artifacts', self.root / 'build/darling-src/build/src/external/libplatform/'
                                     'libsystem_platform.dylib'),
            ('config', self.root / 'build/config.json'),
            ('tree', self.root / 'build/darling-src/source.c'),
            ('overlays', self.root / 'prefix/usr/lib/libGL.dylib'),
        )
        for section, path in changes:
            with self.subTest(section=section):
                self.record()
                self.assertEqual(0, self.check())
                original = path.read_bytes()
                path.write_bytes(original + b'\nchanged')
                self.assertEqual(1, self.check())
                path.write_bytes(original)
                self.record()

    def test_deleted_overlay_forces_a_rebuild(self):
        self.record()
        (self.root / 'prefix/Users/tester/RuntimeLibraries/libsystem_c.dylib').unlink()
        self.assertEqual(1, self.check())

    def test_patch_added_untracked_files_are_part_of_the_tree(self):
        # Patches create files with `new file mode`, so `git diff HEAD` alone
        # does not see them; editing one must still invalidate the stamp.
        self.record()
        added = self.root / 'build/darling-src/src/added-header.h'
        added.parent.mkdir(parents=True)
        added.write_text('#define VALUE 1\n')
        self.assertEqual(1, self.check())
        self.record()
        self.assertEqual(0, self.check())
        added.write_text('#define VALUE 2\n')
        self.assertEqual(1, self.check())

    def test_missing_artifact_cannot_be_recorded(self):
        (self.root / 'build/darling-src/build/src/native/libGL.dylib').unlink()
        self.assertEqual(1, stamp.record(self.state, stamp.collect(self.root)))
        self.assertFalse(self.state.exists())

    def test_environment_prefix_is_part_of_the_stamp(self):
        self.record()
        with patch.dict(os.environ, {'DPREFIX': str(self.root / 'other-prefix')}):
            self.assertEqual(1, self.check())

    def test_capabilities_are_hashed(self):
        self.assertRegex(stamp.capabilities_digest(), '^[0-9a-f]{64}$')
        with patch.object(stamp, 'CAPABILITIES',
                          (('missing', ('definitely-not-a-command',)),)):
            self.assertRegex(stamp.capabilities_digest(), '^[0-9a-f]{64}$')

    def test_cli_checks_the_same_state(self):
        self.assertEqual(0, subprocess.run(
            [sys.executable, str(ROOT / 'tools/runtime-overlay-stamp.py'),
             '--root', str(self.root), '--state', str(self.state), 'record'],
            capture_output=True).returncode)
        self.assertEqual(0, subprocess.run(
            [sys.executable, str(ROOT / 'tools/runtime-overlay-stamp.py'),
             '--root', str(self.root), '--state', str(self.state), 'check'],
            capture_output=True).returncode)


if __name__ == '__main__':
    unittest.main()
