"""Compile tools/test-metal-device-info.cpp against MTLDeviceInfo.h as shipped in patches/metal.patch."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADER = 'private-include/Metal/MTLDeviceInfo.h'


def header_from_patch():
    """MTLDeviceInfo.h is a new file in the patch, so its text is the section's added lines."""
    patch = (ROOT / 'patches/metal.patch').read_text()
    section = re.search(r'(?ms)^diff --git a/%s b/%s\n.*?^@@ [^\n]*\n(.*?)(?=^diff --git |\Z)' % (HEADER, HEADER), patch)
    if not section:
        raise AssertionError(HEADER + ' is not a new file in patches/metal.patch')
    lines = section.group(1).splitlines(keepends=True)
    if not all(line.startswith('+') for line in lines):
        raise AssertionError(HEADER + ' section has context or removed lines')
    return ''.join(line[1:] for line in lines)


class MetalDeviceInfoTests(unittest.TestCase):
    def test_host_check_passes(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('no C++ compiler')
        with tempfile.TemporaryDirectory() as folder:
            include = Path(folder) / 'include/Metal'
            include.mkdir(parents=True)
            (include / 'MTLDeviceInfo.h').write_text(header_from_patch())
            binary = Path(folder) / 'test-metal-device-info'
            build = subprocess.run([compiler, '-std=c++17', '-Wall', '-Werror', '-I', str(include.parent),
                                    str(ROOT / 'tools/test-metal-device-info.cpp'), '-o', str(binary)],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('METAL_DEVICE_INFO failures=0 pass=1', result.stdout)
            self.assertIn('PASS registry ID is non-zero', result.stdout)
            self.assertIn('PASS Mac GPU name', result.stdout)
            self.assertIn('PASS GPU family queries', result.stdout)
            self.assertIn('PASS feature set queries', result.stdout)

    def test_mtldevice_delegates_family_and_feature_set_queries(self):
        patch = (ROOT / 'patches/metal.patch').read_text()
        self.assertIn(
            '+- (BOOL)supportsFamily: (NSUInteger)family { return MTLDeviceInfo::supportsFamily(family) ? YES : NO; }',
            patch,
        )
        self.assertIn(
            '+- (BOOL)supportsFeatureSet: (NSUInteger)featureSet { return MTLDeviceInfo::supportsFeatureSet(featureSet) ? YES : NO; }',
            patch,
        )
        self.assertNotIn('+- (BOOL)supportsFamily: (NSUInteger)family { return YES; }', patch)
        self.assertNotIn('+- (BOOL)supportsFeatureSet: (NSUInteger)featureSet { return YES; }', patch)


if __name__ == '__main__':
    unittest.main()
