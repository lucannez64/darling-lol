"""Offline contracts for the Riot Client 142 fixes ported from #157."""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KERNEL = 'darling/src/libsystem_kernel/emulation/'
SELFUSAGE_NEW = [KERNEL + 'include/xnu_syscall/bsd/impl/misc/thread_selfusage.h',
                 KERNEL + 'src/xnu_syscall/bsd/impl/misc/thread_selfusage.c']


def patch_section(patch, path):
    """Every section of the patch for path, joined: a path may appear twice."""
    marker = f'diff --git a/{path} b/{path}\n'
    text = (ROOT / patch).read_text()
    parts = text.split(marker)[1:]
    if not parts:
        return None
    return '\n'.join(part.split('\ndiff --git ', 1)[0] for part in parts)


def added_lines(section):
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


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


class RiotClient142RemainingTests(unittest.TestCase):
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
        # The 0x30 clear matches an original line, so it is context, not added.
        for offset in ('', '0x10', '0x20'):
            self.assertIn(f'movapd %xmm0, {offset}(%rsp)', source)
        self.assertIn('movq   %rsp, %rsi', source)

    def test_forwarding_frame_matches_the_method_signature_layout(self):
        # NSMethodSignature reads stack arguments at frame + 0xe0: the frame
        # handed to ____forwarding___ must end 0x10 below them (saved rbp and
        # return address), so the return storage has to sit below the frame.
        source = added_lines(patch_section('patches/corefoundation.patch', 'CFForwardingPrep.S'))
        self.assertIn('subq   $0x110, %rsp', source)
        self.assertIn('leaq   0x40(%rsp), %rdi', source)
        self.assertEqual(0x110 - 0x40, 0xe0 - 0x10)
        self.assertIn('movq   %rdi, 0x40(%rsp)', source)
        self.assertIn('movq   0xf0(%rsp), %rax', source)
        self.assertNotIn('subq   $0xf0, %rsp', source)

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

    def test_nsview_change_is_a_separate_section(self):
        # The NSView.m change follows main's section instead of rewriting it,
        # so the hunk headers other patches edit there stay the same.
        text = (ROOT / 'patches/cocotron.patch').read_text()
        self.assertEqual(text.count('diff --git a/AppKit/NSView.m b/AppKit/NSView.m\n'), 2)

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
        # A fixed "@@:" made NSInvocation reject stub setters with arguments:
        # the helper appends one object type per colon of the selector.
        stubs = {'patches/darling.patch': ['src/frameworks/AVFoundation/src/AVFoundation.m',
                                          'src/frameworks/AVFAudio/src/AVFAudio.m',
                                          'src/frameworks/GameController/src/GameController.m'],
                 'patches/metal.patch': ['src/Metal/Metal.mm']}
        for patch, paths in stubs.items():
            for path in paths:
                source = added_lines(patch_section(patch, path))
                self.assertIn('rclolStubSignature(SEL sel)', source, path)
                self.assertIn('rclolStubSignature(sel)', source, path)
                self.assertNotIn('signatureWithObjCTypes:"@@:"]', source, path)
                helper = source.split('rclolStubSignature(SEL sel)', 1)[1].split('\n}', 1)[0]
                self.assertIn('char types[64] = "@@:";', helper, path)
                self.assertIn("if (*c == ':')", helper, path)
                self.assertIn("types[length++] = '@';", helper, path)

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

    def test_ported_parts_do_not_redefine_what_main_has(self):
        # main provides these classes and functions already; the port must not
        # define them a second time.
        cocotron = added_lines((ROOT / 'patches/cocotron.patch').read_text())
        self.assertEqual(cocotron.count('@implementation CAEDRMetadata'), 1)
        self.assertNotIn('RCLOL_NIL_STUB_CLASS(CAEDRMetadata)', cocotron)
        self.assertNotIn('RCLOL_NIL_STUB_CLASS(NSColorSampler)', cocotron)
        self.assertEqual(cocotron.count('\nCGColorSpaceRef CGColorSpaceCreateICCBased('), 1)
        self.assertEqual(cocotron.count('\nconst CFStringRef kCGColorSpaceExtendedLinearITUR_2020'), 1)
        corefoundation = added_lines((ROOT / 'patches/corefoundation.patch').read_text())
        self.assertEqual(corefoundation.count('NSString *const NSURLContentTypeKey'), 1)


if __name__ == '__main__':
    unittest.main()
