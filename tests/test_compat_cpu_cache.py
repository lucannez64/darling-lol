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


if __name__ == '__main__':
    unittest.main()
