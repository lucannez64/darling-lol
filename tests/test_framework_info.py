"""Host-only checks for macOS framework metadata in disposable prefix overlays."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/provision-framework-info.py'
DATA = ROOT / 'tools/data/macos-15.5-framework-info.json'
WEBKIT = 'System/Library/Frameworks/WebKit.framework'
AGL = 'System/Library/Frameworks/AGL.framework'


class FrameworkInfoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / 'prefix with spaces'
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()

    def bundle(self, name=WEBKIT, version='A'):
        path = self.runtime / name
        if version is None:
            (path / 'Resources').mkdir(parents=True)
        else:
            (path / 'Versions' / version).mkdir(parents=True)
            (path / 'Versions/Current').symlink_to(version)
        return path

    def run_tool(self, *args, script=SCRIPT):
        return subprocess.run([sys.executable, str(script), '--prefix', str(self.prefix),
                               '--runtime-root', str(self.runtime), *args],
                              capture_output=True, text=True)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_data_contains_only_factual_metadata_and_expected_versions(self):
        raw = DATA.read_text()
        payload = json.loads(raw)
        self.assertEqual(raw, json.dumps(payload, indent=2, sort_keys=True) + '\n')
        self.assertEqual((payload['schema'], payload['macos'], payload['build']), (1, '15.5', '24F74'))
        allowed = {'CFBundleIdentifier', 'CFBundleName', 'CFBundleExecutable', 'CFBundlePackageType',
                   'CFBundleShortVersionString', 'CFBundleVersion', 'CFBundleSignature',
                   'CFBundleDevelopmentRegion', 'CFBundleInfoDictionaryVersion',
                   'CFBundleSupportedPlatforms', 'DTPlatformName', 'DTPlatformVersion',
                   'DTPlatformBuild', 'DTSDKName', 'DTSDKBuild', 'DTXcode', 'DTXcodeBuild',
                   'DTCompiler', 'BuildMachineOSBuild', 'LSMinimumSystemVersion'}
        self.assertTrue(payload['frameworks'])
        for name, values in payload['frameworks'].items():
            with self.subTest(framework=name):
                self.assertFalse(name.startswith('/'))
                self.assertNotIn('..', Path(name).parts)
                self.assertTrue(values['CFBundleIdentifier'])
                self.assertTrue(values.get('CFBundleVersion') or values.get('CFBundleShortVersionString'))
                self.assertLessEqual(set(values), allowed)
                if 'DTPlatformVersion' in values:
                    self.assertEqual(values['DTPlatformVersion'], '15.5')
        self.assertEqual(payload['frameworks'][WEBKIT]['CFBundleVersion'], '20621.2.5.11.8')

    def test_dry_run_lists_changes_without_creating_prefix(self):
        self.bundle()
        result = self.run_tool()
        self.assert_success(result)
        self.assertIn('Info.plist', result.stdout)
        self.assertFalse(self.prefix.exists())

    def test_versioned_framework_uses_current_version_and_xml_plist(self):
        runtime_bundle = self.bundle(version='C')
        result = self.run_tool('--write')
        self.assert_success(result)
        target = self.prefix / WEBKIT / 'Versions/C/Resources/Info.plist'
        self.assertTrue(target.read_bytes().startswith(b'<?xml'))
        metadata = plistlib.loads(target.read_bytes())
        self.assertEqual(metadata['CFBundleVersion'], '20621.2.5.11.8')
        self.assertEqual(metadata['DTPlatformVersion'], '15.5')
        # Framework Info.plist files are world-readable on macOS.
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(os.readlink(self.prefix / WEBKIT / 'Resources'), 'Versions/Current/Resources')
        self.assertFalse((runtime_bundle / 'Versions/C/Resources').exists())
        self.assert_success(self.run_tool('--check'))

    def test_versioned_framework_without_current_falls_back_to_a(self):
        bundle = self.bundle()
        (bundle / 'Versions/Current').unlink()
        self.assert_success(self.run_tool('--write'))
        self.assertTrue((self.prefix / WEBKIT / 'Versions/A/Resources/Info.plist').is_file())

    def test_shallow_framework_and_missing_frameworks(self):
        bundle = self.bundle(AGL, version=None)
        runtime_plist = bundle / 'Resources/Info.plist'
        runtime_plist.write_bytes(b'original runtime metadata')
        self.assert_success(self.run_tool('--write'))
        target = self.prefix / AGL / 'Resources/Info.plist'
        self.assertEqual(plistlib.loads(target.read_bytes())['CFBundleIdentifier'], 'com.apple.agl')
        self.assertEqual(runtime_plist.read_bytes(), b'original runtime metadata')
        self.assertFalse((self.prefix / WEBKIT).exists())

    def test_existing_runtime_resources_link_is_not_shadowed(self):
        bundle = self.bundle()
        (bundle / 'Resources').symlink_to('Versions/Current/Resources')
        self.assert_success(self.run_tool('--write'))
        self.assertFalse((self.prefix / WEBKIT / 'Resources').is_symlink())
        self.assert_success(self.run_tool('--check'))

    def test_existing_prefix_resources_link_is_preserved(self):
        self.bundle()
        bundle = self.prefix / WEBKIT
        bundle.mkdir(parents=True)
        (bundle / 'Resources').symlink_to('Versions/A/Resources')
        self.assert_success(self.run_tool('--write'))
        self.assertEqual(os.readlink(bundle / 'Resources'), 'Versions/A/Resources')
        self.assert_success(self.run_tool('--check'))

    def test_shallow_framework_allows_internal_resources_link(self):
        self.bundle(AGL, version=None)
        bundle = self.prefix / AGL
        (bundle / 'Metadata').mkdir(parents=True)
        (bundle / 'Resources').symlink_to('Metadata')
        self.assert_success(self.run_tool('--write'))
        self.assertTrue((bundle / 'Metadata/Info.plist').is_file())

    def test_symlink_escape_is_rejected_without_writing_any_framework(self):
        self.bundle(AGL, version=None)
        self.bundle()
        outside = self.root / 'outside'
        outside.mkdir()
        bundle = self.prefix / WEBKIT
        bundle.mkdir(parents=True)
        (bundle / 'Versions').symlink_to(outside, target_is_directory=True)
        result = self.run_tool('--write')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inside', result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.prefix / AGL).exists())

    def test_existing_versioned_resources_escape_is_rejected(self):
        self.bundle()
        outside = self.root / 'outside'
        outside.mkdir()
        bundle = self.prefix / WEBKIT
        bundle.mkdir(parents=True)
        (bundle / 'Resources').symlink_to(outside)
        self.assertNotEqual(self.run_tool('--write').returncode, 0)
        self.assertFalse((bundle / 'Versions').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_new_resources_link_cannot_target_an_escaping_prefix_current(self):
        self.bundle()
        outside = self.root / 'outside'
        outside.mkdir()
        versions = self.prefix / WEBKIT / 'Versions'
        versions.mkdir(parents=True)
        (versions / 'Current').symlink_to(outside)
        result = self.run_tool('--write')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((versions / 'A').exists())
        self.assertFalse((self.prefix / WEBKIT / 'Resources').is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_runtime_current_escape_is_rejected(self):
        bundle = self.bundle()
        outside = self.root / 'outside'
        outside.mkdir()
        (bundle / 'Versions/Current').unlink()
        (bundle / 'Versions/Current').symlink_to(outside)
        self.assertNotEqual(self.run_tool('--write').returncode, 0)
        self.assertFalse(self.prefix.exists())

    def test_shallow_resources_escape_is_rejected(self):
        self.bundle(AGL, version=None)
        outside = self.root / 'outside'
        outside.mkdir()
        bundle = self.prefix / AGL
        bundle.mkdir(parents=True)
        (bundle / 'Resources').symlink_to(outside)
        self.assertNotEqual(self.run_tool('--write').returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_destination_plist_symlink_is_rejected(self):
        self.bundle()
        outside = self.root / 'outside.plist'
        outside.write_bytes(b'untouched')
        target = self.prefix / WEBKIT / 'Versions/A/Resources/Info.plist'
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        self.assertNotEqual(self.run_tool('--write').returncode, 0)
        self.assertEqual(outside.read_bytes(), b'untouched')

    def test_runtime_cannot_be_used_as_prefix(self):
        self.bundle()
        result = self.run_tool('--prefix', str(self.runtime), '--write')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.runtime / WEBKIT / 'Versions/A/Resources').exists())

    def test_unchanged_write_is_idempotent_and_changed_overlay_is_replaced(self):
        self.bundle()
        self.assert_success(self.run_tool('--write'))
        target = self.prefix / WEBKIT / 'Versions/A/Resources/Info.plist'
        before = target.stat().st_mtime_ns
        self.assert_success(self.run_tool('--write'))
        self.assertEqual(target.stat().st_mtime_ns, before)
        target.write_bytes(plistlib.dumps({'CFBundleVersion': 'old'}))
        self.assert_success(self.run_tool('--write'))
        self.assertEqual(plistlib.loads(target.read_bytes())['CFBundleVersion'], '20621.2.5.11.8')

    def test_check_detects_missing_and_different_plists_without_writing(self):
        self.bundle()
        self.assertNotEqual(self.run_tool('--check').returncode, 0)
        self.assertFalse(self.prefix.exists())
        self.assert_success(self.run_tool('--write'))
        target = self.prefix / WEBKIT / 'Versions/A/Resources/Info.plist'
        target.write_bytes(b'not the expected plist')
        self.assertNotEqual(self.run_tool('--check').returncode, 0)
        self.assertEqual(target.read_bytes(), b'not the expected plist')

    def test_check_detects_missing_resources_link(self):
        self.bundle()
        self.assert_success(self.run_tool('--write'))
        (self.prefix / WEBKIT / 'Resources').unlink()
        self.assertNotEqual(self.run_tool('--check').returncode, 0)

    def test_existing_oversized_plist_is_rejected(self):
        self.bundle()
        target = self.prefix / WEBKIT / 'Versions/A/Resources/Info.plist'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'x' * 65537)
        result = self.run_tool('--write')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('64 KiB', result.stderr)
        self.assertEqual(target.stat().st_size, 65537)

    def test_running_or_invalid_prefix_marker_prevents_writes(self):
        self.bundle()
        self.prefix.mkdir()
        marker = self.prefix / '.init.pid'
        for value in [str(os.getpid()), 'invalid', '1']:
            with self.subTest(marker=value):
                marker.write_text(value)
                self.assertNotEqual(self.run_tool('--write').returncode, 0)
                self.assertFalse((self.prefix / 'System').exists())
        self.assert_success(self.run_tool())

    def test_write_and_check_are_mutually_exclusive(self):
        self.assertNotEqual(self.run_tool('--check', '--write').returncode, 0)

    def test_rendered_tool_finds_tracked_data(self):
        self.bundle()
        rendered = ROOT / 'build/runtime/tools/provision-framework-info.py'
        self.assertTrue(rendered.is_file(), 'run scripts/configure.py before this test')
        self.assert_success(self.run_tool('--write', script=rendered))
        self.assert_success(self.run_tool('--check', script=rendered))


    def test_install_provisions_after_bundle_copies_and_before_signing(self):
        script = (ROOT / 'scripts/install-runtime.sh').read_text()
        provision = script.index('provision-framework-info.py" --prefix')
        # Later steps copy Darling's own framework bundles (with Darling's own
        # Info.plist) into the prefix; provisioning must run after all of them
        # and before signing seals the bundles.
        for step in ('install-riot-presentation-runtime.sh', 'install-riot-gpu-runtime.sh',
                     'build-riot-electron43-frameworks.py --install',
                     'sync-canonical-runtime-overlays.py'):
            self.assertLess(script.index(step), provision, step)
        self.assertLess(provision, script.index('sign-darling-runtime.sh" $sign_args'))


if __name__ == '__main__':
    unittest.main()
