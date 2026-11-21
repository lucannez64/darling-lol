"""The compat profile's hw.cacheconfig / hw.cachesize come from the identity table.

Under profile=compat every CPU count reports the i7-9750H's 6 cores / 12
threads. The cache sysctls used to read the host's sysfs, so a 16- or 32-thread
host reported more CPUs sharing an L3 than hw.cacheconfig[0] said exist, and
cache sizes that disagreed with cpuid leaf 4 and 0x80000006. These checks
compile the patched helper against the real identity header and table.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EMULATION = 'darling/src/libsystem_kernel/emulation'


def added_lines(path):
    patch = (ROOT / 'patches/xnu.patch').read_text()
    section = patch.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def function(source, signature):
    source += '\n'
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


class CompatCpuCacheTests(unittest.TestCase):
    def test_both_cache_sysctls_go_through_the_profile_switch(self):
        hw = added_lines(f'{EMULATION}/src/xnu_syscall/bsd/helper/misc/sysctl_hw.c')
        dispatch = function(hw, 'static bool cache_level(')
        self.assertIn('darling_compat_profile_enabled()', dispatch)
        self.assertIn('return compat_cache_level(level, size, sharing);', dispatch)
        for handler in ("\nsysctl_handler(handle_cacheconfig)\n{", "\nsysctl_handler(handle_cachesize)\n{"):
            body = function(hw, handler)
            self.assertIn('cache_level(level, &size, &sharing)', body)
            self.assertNotIn('host_cache_level', body)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_compat_caches_match_the_identity_part(self):
        header = added_lines(f'{EMULATION}/include/common/cpu_identity.h')
        table_source = added_lines(f'{EMULATION}/src/common/cpu_identity.c')
        start = table_source.index('#define DARLING_COMPAT_BRAND')
        table = table_source[start:table_source.index('};', start) + 2]
        hw = added_lines(f'{EMULATION}/src/xnu_syscall/bsd/helper/misc/sysctl_hw.c')
        helper = function(hw, 'static bool compat_cache_level(')

        harness = f"""
#include <assert.h>
#include <stdbool.h>
{header}
{table}
const struct darling_cpu_identity* darling_compat_cpu_identity(void) {{ return &g_compat_identity; }}
{helper}
int main(void)
{{
    unsigned long long size = 0;
    unsigned int sharing = 0;
    assert(g_compat_identity.cores == 6 && g_compat_identity.threads == 12);
    assert(compat_cache_level(1, &size, &sharing) && size == 32768 && sharing == 2);
    assert(compat_cache_level(2, &size, &sharing) && size == 262144 && sharing == 2);
    assert(compat_cache_level(3, &size, &sharing) && size == 12582912 && sharing == 12);
    assert(!compat_cache_level(4, &size, &sharing));
    assert(g_compat_identity.max_mhz == 4500 && g_compat_identity.base_mhz == 2600);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'cache.c'
            binary = Path(folder) / 'cache'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True)

    def test_optional_feature_goes_through_compat_profile_switch(self):
        hw = added_lines(f'{EMULATION}/src/xnu_syscall/bsd/helper/misc/sysctl_hw.c')
        body = function(hw, 'static long optional_feature(')
        self.assertIn('darling_compat_profile_enabled()', body)
        self.assertIn('compat_optional_feature(leaf, reg, bit, regs[reg])', body)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_compat_optional_feature_masks_avx512f_and_leaf1(self):
        header = added_lines(f'{EMULATION}/include/common/cpu_identity.h')
        table_source = added_lines(f'{EMULATION}/src/common/cpu_identity.c')
        start = table_source.index('#define DARLING_COMPAT_BRAND')
        table = table_source[start:table_source.index('};', start) + 2]
        hw = added_lines(f'{EMULATION}/src/xnu_syscall/bsd/helper/misc/sysctl_hw.c')
        helper = function(hw, 'static int compat_optional_feature(')

        harness = f"""
#include <assert.h>
{header}
{table}
const struct darling_cpu_identity* darling_compat_cpu_identity(void) {{ return &g_compat_identity; }}
{helper}
int main(void)
{{
    /* Coffee Lake i7-9750H has no AVX-512F, even on a Zen 4/5 or Tiger Lake host. */
    assert(compat_optional_feature(7, 1, 16, 0xffffffffu) == 0);
    /* BMI1 (3), AVX2 (5), BMI2 (8) reflect the host capability. */
    assert(compat_optional_feature(7, 1, 3, 1u << 3) == 1);
    assert(compat_optional_feature(7, 1, 5, 1u << 5) == 1);
    assert(compat_optional_feature(7, 1, 8, 1u << 8) == 1);
    assert(compat_optional_feature(7, 1, 5, 0u) == 0);
    /* Leaf 1 ECX/EDX mask against the compat part's feature words. */
    assert(compat_optional_feature(1, 2, 28, 0xffffffffu) == 1); /* AVX1.0 */
    assert(compat_optional_feature(1, 2, 28, 0u) == 0);
    assert(compat_optional_feature(1, 2, 31, 0xffffffffu) == 0); /* hypervisor bit */
    assert(compat_optional_feature(1, 3, 26, 0xffffffffu) == 1); /* SSE2 */
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'optional.c'
            binary = Path(folder) / 'optional'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True)

    def test_commpage_get_cpu_caps_masks_features_under_compat(self):
        darling_patch = (ROOT / 'patches/darling.patch').read_text()
        section = darling_patch.split(
            'diff --git a/src/startup/mldr/commpage.c b/src/startup/mldr/commpage.c\n', 1
        )[1].split('\ndiff --git ', 1)[0]
        added = '\n'.join(line[1:] for line in section.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        self.assertIn('strcmp(profile, "compat") == 0', added)
        self.assertIn('ecx1 &= 0x7ffafbbfu;', added)
        self.assertIn('edx1 &= 0xbfebfbffu;', added)
        self.assertIn('ebx7 &= 0x000c4b3cu;', added)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_gethostuuid_parses_provisioned_platform_plist_uuid(self):
        gu = added_lines(f'{EMULATION}/src/xnu_syscall/bsd/impl/misc/gethostuuid.c')
        self.assertIn('if (read_platform_plist_uuid(uuid_buf) == 0)', gu)
        is_hex = function(gu, 'static int is_hex_digit(')
        parser = function(gu, 'static int parse_platform_plist_uuid(')

        harness = f"""
#include <assert.h>
#include <string.h>
static inline unsigned char hvalue(char c)
{{
    if (c >= '0' && c <= '9') return (unsigned char)(c - '0');
    if (c >= 'A' && c <= 'F') return (unsigned char)(10 + (c - 'A'));
    if (c >= 'a' && c <= 'f') return (unsigned char)(10 + (c - 'a'));
    return 0;
}}
static int hex2bin(const char* str, unsigned char* uuid_buf)
{{
    for (int i = 0; i < 16; i++)
        uuid_buf[i] = (unsigned char)((hvalue(str[i * 2]) << 4) | hvalue(str[i * 2 + 1]));
    return 0;
}}
{is_hex}
{parser}
int main(void)
{{
    unsigned char out[16];
    const char* valid =
        "<?xml version=\\"1.0\\" encoding=\\"UTF-8\\"?>\\n"
        "<plist version=\\"1.0\\"><dict><key>properties</key><dict>\\n"
        "  <key>IOPlatformUUID</key>\\n"
        "  <string>12345678-9ABC-DEF0-1234-56789ABCDEF0</string>\\n"
        "</dict></dict></plist>\\n";
    const unsigned char expected[16] = {{
        0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xf0,
        0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xf0
    }};
    assert(parse_platform_plist_uuid(valid, (int)strlen(valid), out) == 0);
    assert(memcmp(out, expected, 16) == 0);

    const char* zero = "<key>IOPlatformUUID</key><string>00000000-0000-0000-0000-000000000000</string>";
    assert(parse_platform_plist_uuid(zero, (int)strlen(zero), out) != 0);

    const char* bad = "<key>IOPlatformUUID</key><string>not-a-uuid</string>";
    assert(parse_platform_plist_uuid(bad, (int)strlen(bad), out) != 0);

    const char* missing = "<dict><key>IOPlatformSerialNumber</key><string>ABC</string></dict>";
    assert(parse_platform_plist_uuid(missing, (int)strlen(missing), out) != 0);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'hostuuid.c'
            binary = Path(folder) / 'hostuuid'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
