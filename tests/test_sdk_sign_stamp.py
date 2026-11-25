"""Exercise the signing stamp that lets update.sh skip the SDK signing pass."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'sdk_sign_stamp', ROOT / 'tools/sdk-sign-stamp.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)


def build_fixture(root):
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/update.sh').write_text('#!/bin/sh\n')
    (root / 'tools').mkdir()
    (root / 'tools/sign-darling-runtime.sh').write_text('#!/bin/sh\n')
    sdk = root / 'sdk'
    (sdk / 'usr/lib').mkdir(parents=True)
    (sdk / 'usr/lib/libsystem_kernel.dylib').write_bytes(b'signed binary')
    (sdk / 'usr/sbin').mkdir()
    (sdk / 'usr/sbin/fseventsd').write_bytes(b'signed helper')
    (sdk / 'usr/lib/libsystem_kernel.dylib').chmod(0o755)
    (sdk / 'usr/lib/link.dylib').symlink_to('libsystem_kernel.dylib')
    return sdk


class SdkSignStampTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='sdk-sign-stamp-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.sdk = build_fixture(self.root)
        self.state = self.root / 'build/sdk-sign-stamp.json'

    def record(self):
        return stamp.record(self.state, stamp.collect(self.root, self.sdk))

    def check(self):
        return stamp.check(self.state, stamp.collect(self.root, self.sdk))

    def test_recorded_state_allows_the_fast_path(self):
        self.assertEqual(0, self.record())
        self.assertEqual(0, self.check())
        self.assertEqual(stamp.VERSION, json.loads(self.state.read_text())['version'])

    def test_missing_or_corrupt_state_signs_again(self):
        self.assertEqual(1, self.check())
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{not json')
        self.assertEqual(1, self.check())
        self.state.write_text(json.dumps({'version': stamp.VERSION - 1, 'sections': {}}))
        self.assertEqual(1, self.check())

    def test_signed_files_change_the_stamp(self):
        # Signing rewrites every Mach-O: size, mtime and mode all move.
        self.assertEqual(0, self.record())
        binary = self.sdk / 'usr/lib/libsystem_kernel.dylib'
        binary.write_bytes(b'signed binary plus a signature')
        self.assertEqual(1, self.check())

    def test_mode_change_signs_again(self):
        self.assertEqual(0, self.record())
        (self.sdk / 'usr/sbin/fseventsd').chmod(0o700)
        self.assertEqual(1, self.check())

    def test_added_file_signs_again(self):
        self.assertEqual(0, self.record())
        (self.sdk / 'usr/lib/extra.dylib').write_bytes(b'new')
        self.assertEqual(1, self.check())

    def test_removed_file_signs_again(self):
        self.assertEqual(0, self.record())
        (self.sdk / 'usr/sbin/fseventsd').unlink()
        self.assertEqual(1, self.check())

    def test_relinked_symlink_signs_again(self):
        self.assertEqual(0, self.record())
        link = self.sdk / 'usr/lib/link.dylib'
        link.unlink()
        link.symlink_to('usr/sbin/fseventsd')
        self.assertEqual(1, self.check())

    def test_changed_signer_signs_again(self):
        self.assertEqual(0, self.record())
        (self.root / 'tools/sign-darling-runtime.sh').write_text('#!/bin/sh\n# changed\n')
        self.assertEqual(1, self.check())

    def test_missing_sdk_cannot_be_recorded(self):
        shutil.rmtree(self.sdk)
        self.assertEqual(1, self.record())
        self.assertFalse(self.state.exists())

    def test_cli_uses_the_same_sections(self):
        for command in ('record', 'check'):
            self.assertEqual(0, subprocess.run(
                [sys.executable, str(ROOT / 'tools/sdk-sign-stamp.py'),
                 '--root', str(self.root), '--sdk', str(self.sdk),
                 '--state', str(self.state), command],
                capture_output=True).returncode)

    def test_cli_defaults_the_sdk_from_the_environment(self):
        env = dict(os.environ, DARLING_SDK_DIR=str(self.sdk))
        self.assertEqual(0, subprocess.run(
            [sys.executable, str(ROOT / 'tools/sdk-sign-stamp.py'),
             '--root', str(self.root), '--state', str(self.state), 'record'],
            capture_output=True, env=env).returncode)


if __name__ == '__main__':
    unittest.main()
