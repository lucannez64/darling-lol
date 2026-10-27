"""Offline checks for the macOS 12 symbols and fixes the Electron 43 Riot Client needs."""
import json
import re
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_text(name):
    return (ROOT / 'patches' / name).read_text()


def section(patch, path):
    marker = f'diff --git a/{path} b/{path}\n'
    assert marker in patch, path
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added(text):
    return '\n'.join(line[1:] for line in text.splitlines() if line.startswith('+') and not line.startswith('+++'))


class ManifestTests(unittest.TestCase):
    def test_new_files_are_listed_and_present_in_their_patches(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        components = {c['path']: c for c in manifest['components']}
        expected = {
            '.': ['src/frameworks/QuickLookUI/CMakeLists.txt', 'src/frameworks/Accessibility/src/Accessibility.m',
                  'src/frameworks/CoreHaptics/src/CoreHaptics.m', 'src/frameworks/CoreML/src/CoreML.m',
                  'src/frameworks/MediaPlayer/src/MediaPlayer.m', 'src/CoreAudio/CoreAudio/AudioHardwareBlocks.mm'],
            'src/external/corefoundation': ['NSConstantLiterals.m'],
            'src/external/foundation': ['src/NSConstantNumbers.m'],
            'src/external/cocotron': ['CoreGraphics/CGCompatibility.m', 'CoreGraphics/CGColorSpaceSRGBProfile.inc'],
            'src/external/metal': ['src/Metal/MTLCompatibility.mm'],
        }
        for path, files in expected.items():
            component = components[path]
            patch = patch_text(Path(component['patch']).name)
            for name in files:
                self.assertIn(name, component['new_files'])
                self.assertIn(f'diff --git a/{name} b/{name}\n', patch)

    def test_every_file_a_patch_creates_is_listed(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        for component in manifest['components']:
            patch = ROOT / component['patch']
            if not patch.exists():
                continue
            created = re.findall(r'^diff --git a/(\S+) b/\S+\nnew file mode', patch.read_text(), re.M)
            listed = set(component.get('new_files', []))
            self.assertEqual([name for name in created if name not in listed], [], component['path'])


class SymbolTests(unittest.TestCase):
    def test_constant_literal_classes_are_defined(self):
        collections = added(section(patch_text('corefoundation.patch'), 'NSConstantLiterals.m'))
        numbers = added(section(patch_text('foundation.patch'), 'src/NSConstantNumbers.m'))
        for name in ('NSConstantArray', 'NSConstantDictionary'):
            self.assertIn(f'@implementation {name}', collections)
        for name in ('__NSArray0__struct', '__NSDictionary0__struct'):
            self.assertRegex(collections, rf'Layout {name} = ')
        for name in ('NSConstantIntegerNumber', 'NSConstantDoubleNumber', 'NSConstantFloatNumber'):
            self.assertIn(f'@implementation {name}', numbers)

    def test_quartz_reexports_quicklookui(self):
        quartz = added(section(patch_text('darling.patch'), 'src/frameworks/Quartz/CMakeLists.txt'))
        self.assertIn('reexport(Quartz QuickLookUI ${QuickLookUI_BUILD})', quartz)

    def test_sandbox_extensions_are_exported_through_libsystem(self):
        patch = patch_text('darling.patch')
        system_sandbox = added(section(patch, 'src/sandbox/sandbox.c'))
        libsandbox = added(section(patch, 'src/libsandbox/src/sandbox.c'))
        for name in ('issue_file', 'issue_mach', 'consume', 'release'):
            self.assertRegex(system_sandbox, rf'\bsandbox_extension_{name}\(')
            self.assertNotRegex(libsandbox, rf'\bsandbox_extension_{name}\(')

    def test_stub_classes_also_forward_class_messages(self):
        stubs = [('darling.patch', 'src/frameworks/MediaPlayer/src/MediaPlayer.m'),
                 ('darling.patch', 'src/frameworks/CoreML/src/CoreML.m'),
                 ('darling.patch', 'src/CoreAudio/CoreAudio/AudioHardwareBlocks.mm'),
                 ('metal.patch', 'src/Metal/MTLCompatibility.mm')]
        for name, path in stubs:
            text = added(section(patch_text(name), path))
            self.assertEqual(text.count('+ (void)forwardInvocation:'), text.count('- (void)forwardInvocation:'), path)

    def test_security_exports_the_certificate_chain(self):
        exports = added(section(patch_text('security.patch'), 'OSX/sec/Security/SecExports.exp-in'))
        self.assertIn('_SecTrustCopyCertificateChain', exports)


class KernelTests(unittest.TestCase):
    def test_rlimit_infinity_is_translated_both_ways(self):
        patch = patch_text('xnu.patch')
        base = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/misc/'
        self.assertIn('0x7fffffffffffffffULL', added(section(patch, base + 'getrlimit.c')))
        self.assertIn('lim.rlim_cur = ~0ULL', added(section(patch, base + 'setrlimit.c')))

    def test_sendto_clears_the_secondary_return_register(self):
        patch = patch_text('xnu.patch')
        table = added(section(patch, 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/bsd_syscall_table.c'))
        self.assertIn('[133] = sys_sendto_trap', table)
        self.assertIn('[413] = sys_sendto_nocancel_trap', table)


class ProfileTests(unittest.TestCase):
    def test_embedded_srgb_profile_is_a_well_formed_icc_profile(self):
        text = added(section(patch_text('cocotron.patch'), 'CoreGraphics/CGColorSpaceSRGBProfile.inc'))
        data = bytes(int(value, 16) for value in re.findall(r'0x([0-9a-f]{2})', text))
        size, = struct.unpack('>I', data[:4])
        self.assertEqual(size, len(data))
        self.assertEqual(data[12:24], b'mntrRGB XYZ ')
        self.assertEqual(data[36:40], b'acsp')
        count, = struct.unpack('>I', data[128:132])
        tags = {}
        for i in range(count):
            signature, offset, length = struct.unpack('>4sII', data[132 + 12 * i:144 + 12 * i])
            self.assertLessEqual(offset + length, size)
            tags[signature] = data[offset:offset + 4]
        for colorant in (b'rXYZ', b'gXYZ', b'bXYZ', b'wtpt'):
            self.assertEqual(tags[colorant], b'XYZ ')
        for curve in (b'rTRC', b'gTRC', b'bTRC'):
            self.assertEqual(tags[curve], b'para')


if __name__ == '__main__':
    unittest.main()
