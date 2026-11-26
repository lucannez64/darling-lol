"""Tests for syncing per-user runtime overlays to canonical macOS prefix paths."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_sync_module():
    spec = importlib.util.spec_from_file_location(
        'sync_canonical_runtime_overlays',
        ROOT / 'tools/sync-canonical-runtime-overlays.py',
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CanonicalRuntimeOverlayTests(unittest.TestCase):
    def test_sync_maps_libraries_and_frameworks_to_canonical_paths(self):
        mod = load_sync_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'prefix'
            guest = prefix / 'Users/tester'
            libs = guest / 'RuntimeLibraries'
            fws = guest / 'RuntimeFrameworks'
            libs.mkdir(parents=True)
            fws.mkdir(parents=True)

            # Libraries across all four canonical buckets + a backup file to ignore.
            (libs / 'libsystem_kernel.dylib').write_bytes(b'kernel')
            (libs / 'libindium.dylib').write_bytes(b'indium')
            (libs / 'libc++.1.dylib').write_bytes(b'libcxx')
            (libs / 'libGL.dylib').write_bytes(b'libgl')
            (libs / 'libindium.dylib.before-test').write_bytes(b'backup')

            # Standard framework with symlinks + CoreServices subframework.
            metal_a = fws / 'Metal.framework/Versions/A'
            metal_a.mkdir(parents=True)
            (metal_a / 'Metal').write_bytes(b'metal-bin')
            os.symlink('A', fws / 'Metal.framework/Versions/Current')
            os.symlink('Versions/Current/Metal', fws / 'Metal.framework/Metal')

            carbon_a = fws / 'CarbonCore.framework/Versions/A'
            carbon_a.mkdir(parents=True)
            (carbon_a / 'CarbonCore').write_bytes(b'carbon-bin')

            changed = mod.sync_prefix(prefix, guest)
            self.assertGreater(changed, 0)

            self.assertEqual((prefix / 'usr/lib/system/libsystem_kernel.dylib').read_bytes(), b'kernel')
            self.assertEqual((prefix / 'usr/lib/darling/libindium.dylib').read_bytes(), b'indium')
            self.assertEqual((prefix / 'usr/lib/libc++.1.dylib').read_bytes(), b'libcxx')
            self.assertEqual(
                (prefix / 'System/Library/Frameworks/OpenGL.framework/Versions/A/Libraries/libGL.dylib').read_bytes(),
                b'libgl',
            )
            self.assertFalse((prefix / 'usr/lib/darling/libindium.dylib.before-test').exists())
            self.assertEqual(
                (prefix / 'System/Library/Frameworks/Metal.framework/Versions/A/Metal').read_bytes(),
                b'metal-bin',
            )
            self.assertEqual(
                os.readlink(prefix / 'System/Library/Frameworks/Metal.framework/Versions/Current'),
                'A',
            )
            self.assertEqual(
                (prefix / 'System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/CarbonCore.framework/Versions/A/CarbonCore').read_bytes(),
                b'carbon-bin',
            )

            # Second pass is a no-op so signing caches stay warm.
            self.assertEqual(mod.sync_prefix(prefix, guest), 0)

    def test_a_signed_copy_is_not_rewritten_from_its_unsigned_source(self):
        # Signing appends the same signature to the per-user file and to its
        # canonical copy, so the two stay byte-identical. The mirror must leave
        # that copy alone: install-runtime.sh's signing stamp treats any rewrite
        # of the canonical roots as a reason to sign the whole prefix again.
        mod = load_sync_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'prefix'
            guest = prefix / 'Users/tester'
            libs = guest / 'RuntimeLibraries'
            libs.mkdir(parents=True)
            (libs / 'libfoo.dylib').write_bytes(b'binary')
            self.assertEqual(mod.sync_prefix(prefix, guest), 1)
            signed = b'binary' + b'\0signature'
            (libs / 'libfoo.dylib').write_bytes(signed)
            (prefix / 'usr/lib/libfoo.dylib').write_bytes(signed)
            self.assertEqual(mod.sync_prefix(prefix, guest), 0)
            self.assertEqual((prefix / 'usr/lib/libfoo.dylib').read_bytes(), signed)

    def test_install_runtime_syncs_before_signing(self):
        script = (ROOT / 'scripts/install-runtime.sh').read_text()
        validate = script.index('validate-riot-runtime.py')
        sync = script.index('sync-canonical-runtime-overlays.py')
        sign = script.index('sign-darling-runtime.sh" $sign_args')
        self.assertLess(validate, sync)
        self.assertLess(sync, sign)

    def test_launcher_omits_dyld_vars_when_canonical_overlays_exist(self):
        launcher = (ROOT / 'tools/run-riot-client.sh').read_text()
        self.assertIn('RCLOL_CANONICAL_OVERLAYS', launcher)
        self.assertIn('-u DYLD_LIBRARY_PATH -u DYLD_FRAMEWORK_PATH', launcher)
        self.assertIn('${dyld_library_path:+DYLD_LIBRARY_PATH="$dyld_library_path"}', launcher)
        self.assertIn('${dyld_framework_path:+DYLD_FRAMEWORK_PATH="$dyld_framework_path"}', launcher)


if __name__ == '__main__':
    unittest.main()
