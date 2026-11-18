"""Checks for cpuid_trace Group 1 16.23 addresses and bounded __TEXT,__text pattern fallback."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def cpuid_trace_source():
    patch = (ROOT / 'patches/xnu.patch').read_text()
    marker = ('diff --git a/darling/src/libsystem_kernel/emulation/src/linux_premigration/signal/cpuid_trace.c '
              'b/darling/src/libsystem_kernel/emulation/src/linux_premigration/signal/cpuid_trace.c\n')
    section = patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class CpuidTrace1623Tests(unittest.TestCase):
    def test_group1_table_tracks_16_23_addresses(self):
        src = cpuid_trace_source()
        self.assertIn('0x1014dd5c4UL, 0x1014dd5f8UL, 0x1014dd624UL, 0x1014dd6c6UL', src)
        self.assertIn('rclol_find_text_section', src)
        self.assertIn('rclol_arm_group1_scan', src)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_text_bounds_validation_and_shifted_pattern_fallback(self):
        src = cpuid_trace_source()
        start = src.index('static int rclol_match_tag16(')
        end = src.index('static void rclol_log_uuid(')
        helpers = src[start:end]
        harness = f"""
#include <assert.h>
#include <stdint.h>
#include <string.h>

static unsigned long armed_addr[4];
static unsigned long armed_cookie[4];
static int armed_count = 0;

static int rclol_cpuid_trace_arm(unsigned long addr, unsigned long cookie) {{
    if (armed_count < 4) {{
        armed_addr[armed_count] = addr;
        armed_cookie[armed_count] = cookie;
    }}
    armed_count++;
    return 0;
}}

{helpers}

int main(void) {{
    // Synthetic 64-bit Mach-O image in one contiguous buffer (VM base 0x100000000).
    uint8_t img[0x1000];
    memset(img, 0x90, sizeof(img));

    uint32_t* mh = (uint32_t*) img;
    mh[0] = 0xfeedfacfU; // MH_MAGIC_64
    mh[4] = 1;           // ncmds
    mh[5] = 72 + 80;     // sizeofcmds

    uint8_t* seg = img + 32;
    *(uint32_t*) (seg + 0) = 0x19;       // LC_SEGMENT_64
    *(uint32_t*) (seg + 4) = 72 + 80;    // cmdsize
    memcpy(seg + 8, "__TEXT\\0", 7);
    *(uint32_t*) (seg + 64) = 1;         // nsects

    uint8_t* sect = seg + 72;
    memcpy(sect + 0, "__text\\0", 7);
    memcpy(sect + 16, "__TEXT\\0", 7);
    *(uint64_t*) (sect + 32) = 0x100000200ULL; // unslid __text addr
    *(uint64_t*) (sect + 40) = 0x400ULL;       // __text size -> ends at +0x600

    unsigned long slide = (unsigned long) img - 0x100000000UL;
    unsigned long ts = 0, te = 0;
    assert(rclol_find_text_section(img, slide, &ts, &te) == 1);
    assert(ts == (unsigned long) (img + 0x200));
    assert(te == (unsigned long) (img + 0x600));

    // Out-of-bounds address must be rejected safely without dereferencing.
    assert(rclol_is_cpuid_insn((unsigned long) (img + 0x700), 1, ts, te) == 0);
    assert(rclol_is_cpuid_insn((unsigned long) (img + 0x100), 1, ts, te) == 0);

    // Place the 4-site Group 1 signature at +0x310 inside __text.
    size_t base_off = 0x310;
    size_t deltas[4] = {{0x0, 0x34, 0x60, 0x102}};
    for (int i = 0; i < 4; ++i) {{
        img[base_off + deltas[i]] = 0x0f;
        img[base_off + deltas[i] + 1] = 0xa2;
    }}
    assert(rclol_is_cpuid_insn((unsigned long) (img + base_off), 1, ts, te) == 1);
    assert(rclol_arm_group1_scan(ts, te, slide) == 1);
    assert(armed_count == 4);
    for (int i = 0; i < 4; ++i) {{
        assert(armed_addr[i] == (unsigned long) (img + base_off + deltas[i]));
        assert(armed_cookie[i] == 0x100000000UL + base_off + deltas[i]);
    }}
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            src_path = Path(folder) / 'cpuid_fallback_test.c'
            exe_path = Path(folder) / 'cpuid_fallback_test'
            src_path.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', str(src_path), '-o', str(exe_path)], check=True)
            subprocess.run([str(exe_path)], check=True)


if __name__ == '__main__':
    unittest.main()
