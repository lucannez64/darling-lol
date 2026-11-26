"""The commpage carries hw.cpufamily and the cache line size, as on macOS.

XNU fills _COMM_PAGE_CPUFAMILY and _COMM_PAGE_CACHE_LINESIZE for every x86
process; the loader left both at 0. Under profile=compat they are the
i7-9750H's, and its commpage capabilities have neither HLE nor RTM.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def patched_lines(path):
    """Added and context lines of one file's section."""
    patch = (ROOT / 'patches/darling.patch').read_text()
    section = patch.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith(('+', ' ')) and not line.startswith('+++'))


class CommpageCpuIdentityTests(unittest.TestCase):
    def test_loader_writes_both_fields_after_the_profile_choice(self):
        commpage = patched_lines('src/startup/mldr/commpage.c')
        start = commpage.index('unsigned int signature = 0, ebx1 = 0, c, d;')
        block = commpage[start:commpage.index('*(uint32_t*)CGET(_COMM_PAGE_CPUFAMILY)', start) + 80]
        self.assertIn('__cpuid_count(1, 0, signature, ebx1, c, d);', block)
        self.assertIn('if (compat_profile)\n\t\t{\n\t\t\tsignature = 0x000906eau;\n\t\t\tebx1 = 8u << 8;', block)
        self.assertIn('linesize = commpage_cache_linesize(ebx1);', block)
        self.assertIn('my_caps |= commpage_cache_linesize_cap(linesize);', block)
        self.assertIn('*(uint16_t*)CGET(_COMM_PAGE_CACHE_LINESIZE) = linesize;', block)
        # the capability word is stored after the line-size bit is added
        self.assertLess(commpage.index('my_caps |= commpage_cache_linesize_cap(linesize);'),
                        commpage.index('*cpu_caps64 = my_caps;'))
        self.assertIn('*(uint32_t*)CGET(_COMM_PAGE_CPUFAMILY) = commpage_cpufamily(signature);', block)

    def test_compat_capabilities_have_no_hle_or_rtm(self):
        commpage = patched_lines('src/startup/mldr/commpage.c')
        self.assertIn('ebx7 &= 0x000c432cu;', commpage)
        self.assertEqual(0x000c432c & ((1 << 4) | (1 << 11)), 0)
        # SGX, BMI1, AVX2, BMI2, ERMS, MPX, RDSEED and ADX stay host-gated.
        self.assertEqual(0x000c432c, sum(1 << bit for bit in (2, 3, 5, 8, 9, 14, 18, 19)))

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_cpufamily_and_line_size_decoding(self):
        header = patched_lines('src/startup/mldr/commpage-cpu-caps.h')
        helpers = header[header.index('/* hw.cpufamily from the leaf 1 signature'):header.rindex('#endif')]
        harness = f"""
#include <assert.h>
#include <stdint.h>
/* osfmk/i386/cpu_capabilities.h */
#define kCache32 0x00000010
#define kCache64 0x00000020
#define kCache128 0x00000040
{helpers}
int main(void)
{{
    assert(commpage_cpufamily(0x000906ea) == 0x0f817246); /* i7-9750H */
    assert(commpage_cpufamily(0x000806ea) == 0x0f817246); /* Kaby Lake-R */
    assert(commpage_cpufamily(0x000506e3) == 0x37fc219f); /* Skylake */
    assert(commpage_cpufamily(0x000306c3) == 0x10b282dc); /* Haswell */
    assert(commpage_cpufamily(0x000706e5) == 0x38435547); /* Ice Lake */
    assert(commpage_cpufamily(0x00a20f12) == 0);          /* Zen 3 */
    assert(commpage_cpufamily(0x000b0671) == 0);          /* Raptor Lake: not in XNU */
    assert(commpage_cpufamily(0) == 0);
    assert(commpage_cache_linesize(8u << 8) == 64);
    assert(commpage_cache_linesize(0x050c0800) == 64);
    assert(commpage_cache_linesize(0) == 64);
    assert(commpage_cache_linesize(4u << 8) == 32 && commpage_cache_linesize(16u << 8) == 128);
    assert(commpage_cache_linesize(3u << 8) == 64); /* not a size the capability word names */
    /* the capability bit and the byte count always agree, as in xnu */
    assert(commpage_cache_linesize_cap(64) == kCache64);
    assert(commpage_cache_linesize_cap(32) == kCache32);
    assert(commpage_cache_linesize_cap(128) == kCache128);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'commpage.c'
            binary = Path(folder) / 'commpage'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
