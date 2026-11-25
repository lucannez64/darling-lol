"""Exercise the install stamp that lets update.sh skip `cmake --install`."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'darling_install_stamp', ROOT / 'tools/darling-install-stamp.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)


def build_fixture(root):
    """A built tree with a manifest that installs into a temporary prefix."""
    source = root / 'build/darling-src'
    (source / 'etc').mkdir(parents=True)
    subprocess.run(['git', '-C', str(source), 'init', '-q'], check=True,
                   capture_output=True)
    (source / '.gitignore').write_text('build/\n')
    (source / 'CMakeLists.txt').write_text('project(demo)\n')
    # install(FILES ...) reads this straight from the checkout.
    (source / 'etc/notifyd.plist').write_text('<plist/>\n')
    subprocess.run(['git', '-C', str(source), 'add', '.gitignore',
                    'CMakeLists.txt', 'etc/notifyd.plist'], check=True,
                   capture_output=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-q',
                    '-m', 'fixture'], check=True, capture_output=True)
    build = source / 'build'
    (build / 'lib').mkdir(parents=True)
    (build / 'lib/libdemo.dylib').write_bytes(b'library')
    (build / 'bin').mkdir()
    (build / 'bin/demo').write_bytes(b'executable')
    # Intermediate objects are excluded from the digest on purpose.
    (build / 'lib/CMakeFiles/Demo.dir').mkdir(parents=True)
    (build / 'lib/CMakeFiles/Demo.dir/object.o').write_bytes(b'object')

    prefix = root / 'prefix'
    (prefix / 'libexec').mkdir(parents=True)
    (prefix / 'libexec/libdemo.dylib').write_bytes(b'library')
    (prefix / 'libexec/demo').write_bytes(b'executable')
    manifest = build / 'install_manifest.txt'
    # install-bin is moved away by an install(CODE) rule, so a missing entry is
    # legitimate and has to stay recorded as missing.
    manifest.write_text('\n'.join((
        str(prefix / 'libexec/libdemo.dylib'),
        str(prefix / 'libexec/demo'),
        str(prefix / 'libexec/install-bin'),
        '')))
    return root


class InstallStampTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='install-stamp-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        build_fixture(self.root)
        self.state = self.root / 'build/install-stamp.json'

    def record(self):
        return stamp.record(self.state, stamp.collect(self.root),
                            stamp.manifest_paths(self.root) or [])

    def check(self):
        return stamp.check(self.state, stamp.collect(self.root))

    def test_recorded_state_allows_the_fast_path(self):
        self.assertEqual(0, self.record())
        self.assertEqual(0, self.check())
        payload = json.loads(self.state.read_text())
        self.assertEqual(stamp.VERSION, payload['version'])
        self.assertEqual(3, payload['installed'])

    def test_missing_or_corrupt_state_reinstalls(self):
        self.assertEqual(1, self.check())
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{not json')
        self.assertEqual(1, self.check())
        self.state.write_text(json.dumps({'version': stamp.VERSION - 1, 'sections': {}}))
        self.assertEqual(1, self.check())

    def test_changed_build_outputs_reinstall(self):
        self.assertEqual(0, self.record())
        (self.root / 'build/darling-src/build/lib/libdemo.dylib').write_bytes(b'relinked')
        self.assertEqual(1, self.check())

    def test_changed_install_source_reinstalls(self):
        # install(FILES ...) copies from the checkout, not from the build tree.
        self.assertEqual(0, self.record())
        (self.root / 'build/darling-src/etc/notifyd.plist').write_text('<plist>changed</plist>\n')
        self.assertEqual(1, self.check())

    def test_intermediate_objects_do_not_reinstall(self):
        self.assertEqual(0, self.record())
        (self.root / 'build/darling-src/build/lib/CMakeFiles/Demo.dir/object.o').write_bytes(
            b'object rebuilt')
        self.assertEqual(0, self.check())

    def test_changed_manifest_reinstalls(self):
        self.assertEqual(0, self.record())
        manifest = self.root / 'build/darling-src/build/install_manifest.txt'
        manifest.write_text(manifest.read_text() + '/usr/local/bin/extra\n')
        self.assertEqual(1, self.check())

    def test_removed_destination_reinstalls(self):
        self.assertEqual(0, self.record())
        (self.root / 'prefix/libexec/demo').unlink()
        self.assertEqual(1, self.check())

    def test_missing_manifest_cannot_be_recorded(self):
        (self.root / 'build/darling-src/build/install_manifest.txt').unlink()
        self.assertEqual(1, self.record())
        self.assertFalse(self.state.exists())

    def test_missing_build_tree_cannot_be_recorded(self):
        shutil.rmtree(self.root / 'build/darling-src/build')
        self.assertEqual(1, self.record())
        self.assertFalse(self.state.exists())

    def test_cli_checks_the_same_state(self):
        for command in ('record', 'check'):
            self.assertEqual(0, subprocess.run(
                [sys.executable, str(ROOT / 'tools/darling-install-stamp.py'),
                 '--root', str(self.root), '--state', str(self.state), command],
                capture_output=True).returncode)


if __name__ == '__main__':
    unittest.main()
