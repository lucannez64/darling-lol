"""Offline contracts for the frameworks required by the 2026 Riot Client build."""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

DARLING_FRAMEWORKS = {
    'src/frameworks/QuickLookUI/CMakeLists.txt': ['QLPreviewPanel.m', 'QLPreviewView.m'],
    'src/frameworks/Accessibility/CMakeLists.txt': ['ApplicationServices'],
    'src/frameworks/MediaPlayer/CMakeLists.txt': ['MPNowPlayingInfoCenter', 'MPRemoteCommandCenter'],
    'src/frameworks/CoreHaptics/CMakeLists.txt': ['CHHapticPattern'],
    'src/frameworks/CoreML/CMakeLists.txt': ['MLModel'],
}

FOUNDATION_NEW = 'src/NSConstantIntegerNumber.m'
COREFOUNDATION_NEW = 'NSConstantObjects.m'


def patch_section(patch, path):
    marker = f'diff --git a/{path} b/{path}\n'
    text = (ROOT / patch).read_text()
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added_lines(section):
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class RiotClientFrameworkTests(unittest.TestCase):
    def test_new_framework_sources_are_present_and_manifested(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['patch'] == 'patches/darling.patch')
        new_files = set(component['new_files'])

        for path in DARLING_FRAMEWORKS:
            self.assertIn(path, new_files)
            section = patch_section('patches/darling.patch', path)
            self.assertIsNotNone(section, path)
            self.assertIn('new file mode', section)

    def test_quartz_reexports_quicklookui(self):
        section = patch_section('patches/darling.patch', 'src/frameworks/Quartz/CMakeLists.txt')
        self.assertIsNotNone(section)
        self.assertIn('reexport(Quartz QuickLookUI', added_lines(section))
        self.assertNotIn('QuickLookUI/src/QLPreviewPanel.m', added_lines(section))

    def test_quicklookui_owns_the_preview_classes(self):
        section = patch_section('patches/darling.patch', 'src/frameworks/QuickLookUI/CMakeLists.txt')
        self.assertIsNotNone(section)
        source = added_lines(section)
        for name in DARLING_FRAMEWORKS['src/frameworks/QuickLookUI/CMakeLists.txt']:
            self.assertIn(name, source)

    def test_accessibility_reexports_application_services(self):
        section = patch_section('patches/darling.patch', 'src/frameworks/Accessibility/CMakeLists.txt')
        self.assertIsNotNone(section)
        self.assertIn('reexport(AccessibilityFramework ApplicationServices', added_lines(section))

    def test_mediaplayer_and_haptics_expose_required_symbols(self):
        for path, symbols in {
            'src/frameworks/MediaPlayer/CMakeLists.txt': ['MPMediaItemArtwork', 'MPNowPlayingInfoCenter'],
            'src/frameworks/CoreHaptics/CMakeLists.txt': ['CHHapticEvent', 'CHHapticPattern'],
            'src/frameworks/CoreML/CMakeLists.txt': ['MLModel', 'MLMultiArray'],
        }.items():
            section = patch_section('patches/darling.patch', path)
            self.assertIsNotNone(section)
            # The CMake lists are short; the source files carry the symbols.
            source_path = path.rsplit('/', 1)[0] + '/src/' + path.rsplit('/', 1)[0].rsplit('/', 1)[-1] + '.m'
            source_section = patch_section('patches/darling.patch', source_path)
            self.assertIsNotNone(source_section, source_path)
            source = added_lines(source_section)
            for symbol in symbols:
                self.assertIn(symbol, source)

    def test_constant_object_classes_are_exported(self):
        foundation = patch_section('patches/foundation.patch', FOUNDATION_NEW)
        self.assertIsNotNone(foundation)
        foundation_source = added_lines(foundation)
        for name in ('NSConstantIntegerNumber', 'NSConstantFloatNumber', 'NSConstantDoubleNumber'):
            self.assertIn(name, foundation_source)

        corefoundation = patch_section('patches/corefoundation.patch', COREFOUNDATION_NEW)
        self.assertIsNotNone(corefoundation)
        corefoundation_source = added_lines(corefoundation)
        for name in ('NSConstantArray', 'NSConstantDictionary'):
            self.assertIn(name, corefoundation_source)

        # Code built with constant literals uses the address of these symbols as
        # the empty array and dictionary, so each must be an object whose isa is
        # the constant class, not a variable holding a pointer to one.
        self.assertIn('struct NSConstantArrayLayout __NSArray0__struct = { (Class)&NSConstantArrayClass',
                      corefoundation_source)
        self.assertIn('struct NSConstantDictionaryLayout __NSDictionary0__struct = { (Class)&NSConstantDictionaryClass',
                      corefoundation_source)
        self.assertIsNone(patch_section('patches/corefoundation.patch', 'CFRuntime.c'))

    def test_constant_objects_read_the_layout_clang_emits(self):
        corefoundation_source = added_lines(patch_section('patches/corefoundation.patch', COREFOUNDATION_NEW))
        for method in ('- (NSUInteger)count', '- (id)objectAtIndex:(NSUInteger)index',
                       '- (id)objectForKey:(id)key', '- (NSEnumerator *)keyEnumerator'):
            self.assertIn(method, corefoundation_source)
        foundation_source = added_lines(patch_section('patches/foundation.patch', FOUNDATION_NEW))
        for method in ('- (const char *)objCType', '- (void)getValue:(void *)value',
                       '- (long long)longLongValue', '- (double)doubleValue'):
            self.assertIn(method, foundation_source)

    def test_manifest_lists_constant_object_sources(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        by_patch = {c['patch']: c for c in manifest['components']}
        self.assertIn(FOUNDATION_NEW, by_patch['patches/foundation.patch']['new_files'])
        self.assertIn(COREFOUNDATION_NEW, by_patch['patches/corefoundation.patch']['new_files'])


KERNEL = 'darling/src/libsystem_kernel/emulation/'
SELFUSAGE_NEW = [KERNEL + 'include/xnu_syscall/bsd/impl/misc/thread_selfusage.h',
                 KERNEL + 'src/xnu_syscall/bsd/impl/misc/thread_selfusage.c']


def icc_profile():
    """Build the sRGB profile with the C from the CoreGraphics patch."""
    compiler = shutil.which('cc')
    if compiler is None:
        raise unittest.SkipTest('needs a C compiler')
    source = added_lines(patch_section('patches/cocotron.patch', 'CoreGraphics/CGColorSpace.m'))
    builder = source.split('static void iccPut32', 1)[1].split('CFDataRef CGColorSpaceCopyICCData', 1)[0]
    program = (
        '#include <stdint.h>\n#include <stdio.h>\n'
        'typedef struct { int unused; } *CFDataRef;\n'
        'static CFDataRef CFDataCreate(void *allocator, const uint8_t *bytes, long length) {\n'
        '    fwrite(bytes, 1, length, stdout);\n    return (CFDataRef)1;\n}\n'
        'static void iccPut32' + builder +
        'int main(void) { return createSRGBProfileData() ? 0 : 1; }\n')
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / 'icc.c').write_text(program)
        subprocess.run([compiler, '-o', 'icc', 'icc.c'], cwd=folder, check=True)
        return subprocess.run([str(Path(folder) / 'icc')], check=True, capture_output=True).stdout


class RiotClientRuntimeTests(unittest.TestCase):
    def test_bsd_syscalls_return_zero_in_rdx_except_pipe(self):
        # Chromium's sendto wrapper reads %rdx (XNU's second return value) and
        # reports ERR_NETWORK_CHANGED when it is not zero.
        source = added_lines(patch_section('patches/xnu.patch', KERNEL + 'src/xnu_syscall/bsd/bsd_syscall.S'))
        self.assertIn('cmpq\t$42, %rax', source)
        self.assertIn('xorl\t%edx, %edx', source)
        pipe = (ROOT / 'build/darling-src/src/external/xnu' / KERNEL / 'src/xnu_syscall/bsd/impl/unistd/pipe.c')
        if pipe.exists():
            self.assertIn('%%edx', pipe.read_text())

    def test_thread_selfusage_is_implemented(self):
        table = added_lines(patch_section('patches/xnu.patch', KERNEL + 'src/xnu_syscall/bsd/bsd_syscall_table.c'))
        self.assertIn('[482] = sys_thread_selfusage,', table)
        self.assertIn('thread_selfusage.c', added_lines(patch_section('patches/xnu.patch', KERNEL + 'CMakeLists.txt')))
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['patch'] == 'patches/xnu.patch')
        for path in SELFUSAGE_NEW:
            self.assertIn(path, component['new_files'])
            self.assertIn('new file mode', patch_section('patches/xnu.patch', path))

    def test_renderer_sysctls_exist(self):
        source = added_lines(patch_section('patches/xnu.patch',
                                           KERNEL + 'src/xnu_syscall/bsd/helper/misc/sysctl_kern.c'))
        self.assertIn('.name = "hv_vmm_present", .handler = handle_hv_vmm_present', source)
        # It mirrors the host's CPUID hypervisor bit rather than a fixed value,
        # and checks the caller's buffer before writing it.
        handler = source.split('static sysctl_handler(handle_hv_vmm_present) {', 1)[1].split('\n}', 1)[0]
        self.assertIn('value = (ecx >> 31) & 1;', handler)
        self.assertLess(handler.index('sysctl_handle_size(sizeof(int));'), handler.index('*((int*) old) = value;'))

    def test_unlimited_rlimits_use_the_darwin_value(self):
        source = added_lines(patch_section('patches/xnu.patch', KERNEL + 'src/xnu_syscall/bsd/impl/misc/getrlimit.c'))
        self.assertIn('rlimit_linux_to_bsd_value(rlp->rlim_cur)', source)
        header = added_lines(patch_section('patches/xnu.patch', KERNEL + 'include/conversion/misc/getrlimit.h'))
        self.assertIn('BSD_RLIM_INFINITY 0x7fffffffffffffffULL', header)

    def test_srgb_has_a_valid_icc_profile(self):
        profile = icc_profile()
        number = lambda offset: struct.unpack_from('>I', profile, offset)[0]
        fixed = lambda offset: struct.unpack_from('>i', profile, offset)[0] / 65536
        self.assertEqual(number(0), len(profile))
        self.assertEqual(profile[12:24], b'mntrRGB XYZ ')
        self.assertEqual(profile[36:40], b'acsp')
        tags = {}
        for index in range(number(128)):
            entry = 132 + index * 12
            offset, size = number(entry + 4), number(entry + 8)
            self.assertLessEqual(offset + size, len(profile))
            tags[profile[entry:entry + 4]] = offset
        for sig in (b'rTRC', b'gTRC', b'bTRC'):
            self.assertEqual(profile[tags[sig]:tags[sig] + 4], b'para')
            self.assertAlmostEqual(fixed(tags[sig] + 12), 2.4, places=3)
        # The D50-adapted primaries add up to the D50 white point.
        for axis in range(3):
            total = sum(fixed(tags[sig] + 8 + axis * 4) for sig in (b'rXYZ', b'gXYZ', b'bXYZ'))
            self.assertAlmostEqual(total, fixed(tags[b'wtpt'] + 8 + axis * 4), places=3)

    def test_named_rgb_spaces_are_not_null(self):
        source = added_lines(patch_section('patches/cocotron.patch', 'Onyx2D/O2ColorSpace.m'))
        for name in ('kCGColorSpaceExtendedSRGB', 'kCGColorSpaceLinearSRGB', 'kCGColorSpaceGenericGray'):
            self.assertIn(f'CFSTR("{name}")', source)

    def test_loaded_bundles_load_again(self):
        source = added_lines(patch_section('patches/foundation.patch', 'src/NSBundle.m'))
        self.assertIn('if ([self isLoaded])', source)
        self.assertIn('loaded = true;', source)

    def test_forwarding_clears_return_storage(self):
        source = added_lines(patch_section('patches/corefoundation.patch', 'CFForwardingPrep.S'))
        self.assertIn('subq   $0xf0, %rsp', source)
        for offset in ('0xc0', '0xd0', '0xe0'):
            self.assertIn(f'movapd %xmm0, {offset}(%rsp)', source)

    def test_struct_returning_window_methods_exist(self):
        # Darling's forwarding has no stret path, so a missing NSRect method
        # crashes instead of raising.
        window = added_lines(patch_section('patches/cocotron.patch', 'AppKit/NSWindow.m'))
        for method in ('- (NSRect) contentLayoutRect {',
                       '- (NSRect) backingAlignedRect: (NSRect) rect options: (NSAlignmentOptions) options {',
                       '- (NSRect) accessibilityFrame {'):
            self.assertIn(method, window)
        view = added_lines(patch_section('patches/cocotron.patch', 'AppKit/NSView.m'))
        self.assertIn('- (NSRect) backingAlignedRect: (NSRect) rect options: (NSAlignmentOptions) options {', view)
        self.assertIn('- (NSRect) accessibilityFrame {', view)

    def test_frameless_windows_can_be_dragged(self):
        # Electron drags the window when this private method answers -1.
        source = added_lines(patch_section('patches/cocotron.patch', 'AppKit/NSWindow.m'))
        method = source.split('- (NSInteger) _resizeDirectionForMouseLocation: (NSPoint) location {', 1)[1]
        self.assertIn('return -1;', method.split('}', 1)[0])

    def test_url_handlers_and_activation_are_answered(self):
        # The Riot Client asks for its URL handler right after sign-in.
        workspace = added_lines(patch_section('patches/cocotron.patch', 'AppKit/MacWorkspace.m'))
        self.assertIn('- (NSURL *) URLForApplicationToOpenURL: (NSURL *) url {', workspace)
        self.assertIn('LSCopyDefaultApplicationURLForURL', workspace)
        self.assertIn('- (NSArray<NSURL *> *) URLsForApplicationsToOpenURL: (NSURL *) url {', workspace)
        running = added_lines(patch_section('patches/cocotron.patch', 'AppKit/NSRunningApplication.m'))
        self.assertIn('+ (NSRunningApplication *) currentApplication {', running)
        application = added_lines(patch_section('patches/cocotron.patch', 'AppKit/NSApplication.m'))
        self.assertIn('- (void) yieldActivationToApplicationWithBundleIdentifier: (NSString *) bundleIdentifier {',
                      application)

    def test_tool_tip_rects_hand_chromium_an_owner(self):
        # Chromium captures the owner from -addTrackingRect: and hides its tool
        # tip by sending it mouseExited:.
        source = added_lines(patch_section('patches/cocotron.patch', 'AppKit/NSView.m'))
        self.assertIn('@implementation NSToolTipRectOwner', source)
        self.assertIn('- (void) mouseExited: (NSEvent *) event {', source)
        self.assertIn('owner: [NSToolTipRectOwner sharedOwner]', source)
        self.assertIn('hideToolTipOfArea(area);', source)

    def test_contents_of_url_loaders_accept_a_null_error(self):
        for path in ('NSDictionary.m', 'NSArray.m'):
            source = added_lines(patch_section('patches/corefoundation.patch', path))
            self.assertIn('if (tmp == nil && error != NULL) {', source)
            self.assertNotIn('if (tmp == nil) {', source)

    def test_log2_is_exact_for_powers_of_two(self):
        # Chromium's WebAudio FFT truncates log2 of its size and CHECKs that
        # the power of two matches.
        source = added_lines(patch_section('patches/darling.patch', 'src/libm/Source/Intel/xmm_log.c'))
        self.assertIn('double mantissa = frexp( x, &exponent );', source)
        self.assertIn('return (double) exponent + log( mantissa ) * conversion;', source)

    def test_metal_device_lists_no_counter_sets(self):
        # Dawn queries this while probing WebGPU; an exception kills the GPU process.
        source = added_lines(patch_section('patches/metal.patch', 'src/Metal/MTLDevice.mm'))
        self.assertIn('- (NSArray *)counterSets { return @[]; }', source)

    def test_stub_signatures_follow_the_selector(self):
        # A fixed "@@:" made NSInvocation reject stub setters with arguments.
        stubs = {'patches/darling.patch': ['src/frameworks/MediaPlayer/src/MediaPlayer.m',
                                          'src/frameworks/AVFoundation/src/AVFoundation.m',
                                          'src/frameworks/AVFAudio/src/AVFAudio.m',
                                          'src/frameworks/GameController/src/GameController.m',
                                          'src/frameworks/CoreML/src/CoreML.m',
                                          'src/frameworks/CoreHaptics/src/CoreHaptics.m',
                                          'src/frameworks/Accessibility/src/AXCustomContent.m'],
                 'patches/cocotron.patch': ['AppKit/NSAccessibility.m', 'QuartzCore/CALayer.m'],
                 'patches/metal.patch': ['src/Metal/Metal.mm']}
        for patch, paths in stubs.items():
            for path in paths:
                source = added_lines(patch_section(patch, path))
                self.assertIn('rclolStubSignature(SEL sel)', source, path)
                self.assertIn('rclolStubSignature(sel)', source, path)
                self.assertNotIn('signatureWithObjCTypes:"@@:"]', source, path)
        # Known selectors with scalar arguments keep their declared types.
        for path, entry in (('AppKit/NSAccessibility.m', '{ "configurationWithPointSize:weight:scale:", "@@:ddq" }'),
                            ('QuartzCore/CALayer.m',
                             '{ "HDR10MetadataWithDisplayInfo:contentInfo:opticalOutputScale:", "@@:@@f" }')):
            self.assertIn(entry, added_lines(patch_section('patches/cocotron.patch', path)))
        player = added_lines(patch_section('patches/darling.patch', 'src/frameworks/MediaPlayer/src/MediaPlayer.m'))
        self.assertIn('@synthesize nowPlayingInfo = _nowPlayingInfo;', player)
        self.assertIn('@synthesize playbackState = _playbackState;', player)

    def test_metal_reports_the_device_formats(self):
        device = added_lines(patch_section('patches/metal.patch', 'src/Metal/MTLDevice.mm'))
        self.assertIn('vkGetPhysicalDeviceFormatProperties', device)
        self.assertIn('haveFeatures: VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT', device)
        header = added_lines(patch_section('patches/metal.patch', 'include/Metal/MTLDevice.h'))
        for query in ('BOOL supports32BitFloatFiltering;', 'BOOL supportsBCTextureCompression;',
                      'MTLDeviceLocation location;', 'NSArray* counterSets;'):
            self.assertIn(query, header)

    def test_certificate_chain_is_null_without_evaluation(self):
        source = added_lines(patch_section('patches/security.patch', 'OSX/sec/Security/SecTrust.c'))
        function = source.split('CFArrayRef SecTrustCopyCertificateChain(SecTrustRef trust) {', 1)[1].split('\n}', 1)[0]
        self.assertNotIn('_certificates', function)

    def test_reuse_errors_survive_bad_input_and_old_kernels(self):
        source = added_lines(patch_section('patches/xnu.patch',
                                           KERNEL + 'src/xnu_syscall/bsd/impl/mman/madvise.c'))
        self.assertIn('static int populate_write_supported(void)', source)
        self.assertIn('(ret == -LINUX_EINVAL && populate_write_supported())', source)
        self.assertIn("if (((unsigned long) addr & 4095) != 0)", source)

    def test_sandbox_profiles_use_the_seatbelt_layout(self):
        source = added_lines(patch_section('patches/darling.patch', 'src/libsandbox/src/sandbox.c'))
        self.assertIn('struct darling_sandbox_profile', source)
        for field in ('char *builtin;', 'unsigned char *data;', 'size_t size;'):
            self.assertIn(field, source)


if __name__ == '__main__':
    unittest.main()
