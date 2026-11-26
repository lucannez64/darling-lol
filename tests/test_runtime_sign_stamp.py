"""Exercise the stamp that lets install-runtime.sh skip the signing pass."""
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
    'runtime_sign_stamp', ROOT / 'tools/runtime-sign-stamp.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)


def build_fixture(root):
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/update.sh').write_text('#!/bin/sh\n')
    (root / 'tools').mkdir()
    (root / 'tools/sign-darling-runtime.sh').write_text('#!/bin/sh\n')
    store = root / 'store'
    store.write_bytes(b'trust store')
    (root / 'tools/dc-trustcache.py').write_text(
        'import sys\nsys.stdout.write(%r + "\\n")\n' % str(store))
    prefix = root / 'prefix'
    libs = prefix / 'Users/alice/RuntimeLibraries'
    libs.mkdir(parents=True)
    (libs / 'libfoo.dylib').write_bytes(b'signed overlay')
    framework = prefix / 'Users/alice/RuntimeFrameworks/Foo.framework/Versions/A'
    framework.mkdir(parents=True)
    (framework / 'Foo').write_bytes(b'signed framework')
    (prefix / 'usr/lib').mkdir(parents=True)
    (prefix / 'usr/lib/libsystem_kernel.dylib').write_bytes(b'signed kernel')
    (prefix / 'usr/lib/libsystem_kernel.dylib').chmod(0o755)
    (prefix / 'usr/lib/link.dylib').symlink_to('libsystem_kernel.dylib')
    canonical = prefix / 'System/Library/Frameworks/Bar.framework/Versions/A'
    canonical.mkdir(parents=True)
    (canonical / 'Bar').write_bytes(b'signed canonical')
    component = prefix / 'Users/alice/Library/Audio/Plug-Ins/Components/C.component/Contents/MacOS'
    component.mkdir(parents=True)
    (component / 'C').write_bytes(b'signed component')
    (prefix / 'Users/alice/libDarlingXShm.dylib').write_bytes(b'signed xshm')
    (prefix / 'Users/alice/libDarlingWayland.dylib').write_bytes(b'signed wayland')
    sdk = root / 'sdk'
    (sdk / 'usr/lib').mkdir(parents=True)
    (sdk / 'usr/lib/libdarling.dylib').write_bytes(b'signed sdk')
    return prefix, sdk, store


class RuntimeSignStampTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='runtime-sign-stamp-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.prefix, self.sdk, self.store = build_fixture(self.root)
        self.state = self.root / 'build/runtime-sign-stamp.json'

    def record(self, sdk=None):
        return stamp.record(self.state, stamp.collect(self.root, self.prefix, sdk))

    def check(self, sdk=None):
        return stamp.check(self.state, stamp.collect(self.root, self.prefix, sdk))

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
        overlay = self.prefix / 'Users/alice/RuntimeLibraries/libfoo.dylib'
        overlay.write_bytes(b'signed overlay plus a signature')
        self.assertEqual(1, self.check())

    def test_mode_change_signs_again(self):
        self.assertEqual(0, self.record())
        (self.prefix / 'usr/lib/libsystem_kernel.dylib').chmod(0o700)
        self.assertEqual(1, self.check())

    def test_added_file_signs_again(self):
        self.assertEqual(0, self.record())
        (self.prefix / 'Users/alice/RuntimeLibraries/libextra.dylib').write_bytes(b'new')
        self.assertEqual(1, self.check())

    def test_removed_file_signs_again(self):
        self.assertEqual(0, self.record())
        (self.prefix / 'Users/alice/libDarlingXShm.dylib').unlink()
        self.assertEqual(1, self.check())

    def test_relinked_symlink_signs_again(self):
        self.assertEqual(0, self.record())
        link = self.prefix / 'usr/lib/link.dylib'
        link.unlink()
        link.symlink_to('libfoo.dylib')
        self.assertEqual(1, self.check())

    def test_a_new_root_signs_again(self):
        self.assertEqual(0, self.record())
        bob = self.prefix / 'Users/bob/RuntimeLibraries'
        bob.mkdir(parents=True)
        (bob / 'libbob.dylib').write_bytes(b'signed for bob')
        self.assertEqual(1, self.check())

    def test_a_removed_root_signs_again(self):
        self.assertEqual(0, self.record())
        shutil.rmtree(self.prefix / 'Users/alice/RuntimeFrameworks')
        self.assertEqual(1, self.check())

    def test_changed_signer_signs_again(self):
        self.assertEqual(0, self.record())
        (self.root / 'tools/sign-darling-runtime.sh').write_text('#!/bin/sh\n# changed\n')
        self.assertEqual(1, self.check())

    def test_another_prefix_does_not_reuse_the_stamp(self):
        self.assertEqual(0, self.record())
        other = self.root / 'other-prefix'
        shutil.copytree(self.prefix, other)
        self.assertEqual(1, stamp.check(
            self.state, stamp.collect(self.root, other)))

    def test_the_sdk_and_the_trust_store_are_covered_when_selected(self):
        self.assertEqual(0, self.record(sdk=self.sdk))
        self.assertEqual(0, self.check(sdk=self.sdk))
        (self.sdk / 'usr/lib/libdarling.dylib').write_bytes(b'signed sdk plus a signature')
        self.assertEqual(1, self.check(sdk=self.sdk))

    def test_a_replaced_trust_store_signs_again(self):
        self.assertEqual(0, self.record(sdk=self.sdk))
        self.store.write_bytes(b'trust store with another entry')
        self.assertEqual(1, self.check(sdk=self.sdk))

    def test_a_removed_trust_store_signs_again(self):
        self.assertEqual(0, self.record(sdk=self.sdk))
        self.store.unlink()
        self.assertEqual(1, self.check(sdk=self.sdk))

    def test_a_prefix_only_stamp_ignores_the_sdk_and_the_store(self):
        self.assertEqual(0, self.record())
        (self.sdk / 'usr/lib/libdarling.dylib').write_bytes(b'changed')
        self.store.write_bytes(b'changed')
        self.assertEqual(0, self.check())

    def test_missing_signer_cannot_be_recorded(self):
        (self.root / 'tools/sign-darling-runtime.sh').unlink()
        self.assertEqual(1, self.record())
        self.assertFalse(self.state.exists())

    def test_the_root_patterns_match_the_signer(self):
        signer = (ROOT / 'tools/sign-darling-runtime.sh').read_text()
        for pattern in stamp.PREFIX_ROOTS:
            self.assertIn(pattern, signer)
        self.assertIn(stamp.DEFAULT_SDK, signer)

    def test_cli_uses_the_same_sections(self):
        base = [sys.executable, str(ROOT / 'tools/runtime-sign-stamp.py'),
                '--root', str(self.root), '--prefix', str(self.prefix),
                '--state', str(self.state)]
        self.assertEqual(0, subprocess.run(base + ['record'], capture_output=True).returncode)
        self.assertEqual(0, subprocess.run(base + ['check'], capture_output=True).returncode)
        self.assertEqual(0, subprocess.run(
            base + ['--sdk', '--sdk-dir', str(self.sdk), 'record'],
            capture_output=True).returncode)

    def test_cli_defaults_the_prefix_from_the_environment(self):
        env = dict(os.environ, DPREFIX=str(self.prefix))
        self.assertEqual(0, subprocess.run(
            [sys.executable, str(ROOT / 'tools/runtime-sign-stamp.py'),
             '--root', str(self.root), '--state', str(self.state), 'record'],
            capture_output=True, env=env).returncode)

    def test_cli_derives_the_root_when_it_is_omitted(self):
        for command in ('record', 'check'):
            self.assertEqual(0, subprocess.run(
                [sys.executable, str(ROOT / 'tools/runtime-sign-stamp.py'),
                 '--prefix', str(self.prefix), '--state', str(self.state), command],
                capture_output=True).returncode)

    def test_cli_covers_the_selected_sdk(self):
        # `--sdk` takes no value, so it can sit before the command and the
        # separate --sdk-dir keeps the test off the host's real SDK.
        base = [sys.executable, str(ROOT / 'tools/runtime-sign-stamp.py'),
                '--root', str(self.root), '--prefix', str(self.prefix),
                '--sdk', '--sdk-dir', str(self.sdk), '--state', str(self.state)]
        self.assertEqual(0, subprocess.run(base + ['record'], capture_output=True).returncode)
        self.assertEqual(0, subprocess.run(base + ['check'], capture_output=True).returncode)
        (self.sdk / 'usr/lib/libdarling.dylib').write_bytes(b'changed')
        self.assertEqual(1, subprocess.run(base + ['check'], capture_output=True).returncode)


class InstallRuntimeGateTests(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / 'scripts/install-runtime.sh').read_text()

    def test_the_gate_runs_before_the_signer(self):
        gate = self.script.index('runtime-sign-stamp.py')
        signer = self.script.index('tools/sign-darling-runtime.sh" $sign_args')
        self.assertLess(gate, signer)
        self.assertIn('if [ -x "$repo/tools/sign-darling-runtime.sh" ]; then',
                      self.script[:gate])

    def test_the_stamp_is_recorded_only_after_a_successful_pass(self):
        self.assertIn('signing_ok=1\n      python3 build/runtime/tools/runtime-sign-stamp.py',
                      self.script)
        self.assertIn('--state "$sign_stamp" record', self.script)
        self.assertIn('--state "$sign_stamp" check', self.script)

    def test_a_writable_sdk_is_part_of_the_stamp(self):
        self.assertIn('stamp_sdk=--sdk', self.script)
        self.assertIn('[ -w /usr/local/libexec/darling ]', self.script)
        self.assertGreaterEqual(self.script.count('$stamp_sdk'), 2)

    def test_force_bypasses_the_stamp(self):
        self.assertIn('${RCLOL_FORCE_SIGN:-0}', self.script)
        self.assertIn('RCLOL_FORCE_SIGN=1', self.script)


if __name__ == '__main__':
    unittest.main()
