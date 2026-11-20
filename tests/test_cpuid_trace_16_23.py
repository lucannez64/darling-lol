"""Checks for cpuid_trace Group 1 16.23 addresses, bounded __TEXT,__text scan, and rollback."""
from pathlib import Path
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
        self.assertIn('rclol_arm_group1_sites', src)
        self.assertIn('rclol_arm_group1_select', src)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_bounds_validation_setup_fallback_and_partial_rollback(self):
        src = cpuid_trace_source()
        start = src.index('static int rclol_match_tag16(')
        end = src.index('static void rclol_log_uuid(')
        helpers = src[start:end]
        harness = f"""
#include <assert.h>
#include <stdint.h>
#include <string.h>

#define RCLOL_MAX_SITES 4
#define __NR_close 3

static int g_site_count = 0;
static int fail_on_call = -1;
static int arm_calls = 0;
static int next_fd = 10;
static unsigned long armed_addr[16];
static unsigned long armed_cookie[16];
static int armed_total = 0;
static int closed_fds[16];
static int closed_count = 0;

static long rclol_sys(long nr, long a1, long a2, long a3, long a4, long a5, long a6) {{
    (void)a2; (void)a3; (void)a4; (void)a5; (void)a6;
    if (nr == __NR_close && closed_count < 16)
        closed_fds[closed_count++] = (int) a1;
    return 0;
}}

static int rclol_cpuid_trace_arm(unsigned long addr, unsigned long cookie) {{
    int idx = arm_calls++;
    if (fail_on_call >= 0 && idx == fail_on_call)
        return -1;
    if (g_site_count >= RCLOL_MAX_SITES)
        return -1;
    int fd = next_fd++;
    g_site_count++;
    if (armed_total < 16) {{
        armed_addr[armed_total] = addr;
        armed_cookie[armed_total] = cookie;
        armed_total++;
    }}
    return fd;
}}

{helpers}

static void init_image(uint8_t* img, size_t img_size, uint64_t seg_vmaddr,
                       uint64_t seg_vmsize, uint64_t sect_addr, uint64_t sect_size) {{
    memset(img, 0x90, img_size);
    uint32_t* mh = (uint32_t*) img;
    mh[0] = 0xfeedfacfU;
    mh[4] = 1;
    mh[5] = 72 + 80;

    uint8_t* seg = img + 32;
    *(uint32_t*) (seg + 0) = 0x19;
    *(uint32_t*) (seg + 4) = 72 + 80;
    memcpy(seg + 8, "__TEXT\\0", 7);
    *(uint64_t*) (seg + 24) = seg_vmaddr;
    *(uint64_t*) (seg + 32) = seg_vmsize;
    *(uint32_t*) (seg + 64) = 1;

    uint8_t* sect = seg + 72;
    memcpy(sect + 0, "__text\\0", 7);
    memcpy(sect + 16, "__TEXT\\0", 7);
    *(uint64_t*) (sect + 32) = sect_addr;
    *(uint64_t*) (sect + 40) = sect_size;
}}

static void reset_mock(void) {{
    g_site_count = 0;
    fail_on_call = -1;
    arm_calls = 0;
    next_fd = 10;
    armed_total = 0;
    closed_count = 0;
}}

int main(void) {{
    uint8_t img[0x1000];
    const size_t deltas[4] = {{0x0, 0x34, 0x60, 0x102}};
    init_image(img, sizeof(img), 0x100000000ULL, 0x800ULL, 0x100000200ULL, 0x400ULL);
    unsigned long slide = (unsigned long) img - 0x100000000UL;

    // __text declared beyond __TEXT vmaddr+vmsize must be rejected.
    uint8_t bad_img[0x1000];
    init_image(bad_img, sizeof(bad_img), 0x100000000ULL, 0x500ULL, 0x100000200ULL, 0x400ULL);
    unsigned long ts = 0, te = 0;
    assert(rclol_find_text_section(bad_img, (unsigned long) bad_img - 0x100000000UL, &ts, &te) == 0);

    // Valid __text bounds.
    assert(rclol_find_text_section(img, slide, &ts, &te) == 1);
    assert(ts == (unsigned long) (img + 0x200));
    assert(te == (unsigned long) (img + 0x600));

    // Place actual 0f a2 opcodes outside __text to verify the bounds guard rejects them.
    img[0x100] = 0x0f; img[0x101] = 0xa2;
    img[0x700] = 0x0f; img[0x701] = 0xa2;
    assert(rclol_is_cpuid_insn((unsigned long) (img + 0x100), 1, ts, te) == 0);
    assert(rclol_is_cpuid_insn((unsigned long) (img + 0x700), 1, ts, te) == 0);

    // 1. Static hit path: static_sites match 0f a2 at +0x220..+0x322; must not scan earlier +0x200.
    const unsigned long static_sites[4] = {{
        0x100000220UL, 0x100000254UL, 0x100000280UL, 0x100000322UL
    }};
    for (int i = 0; i < 4; ++i) {{
        size_t off = (static_sites[i] - 0x100000000UL);
        img[off] = 0x0f; img[off + 1] = 0xa2;
    }}
    reset_mock();
    assert(rclol_arm_group1_select(img, slide, static_sites) == 1);
    assert(g_site_count == 4 && armed_total == 4);
    for (int i = 0; i < 4; ++i)
        assert(armed_cookie[i] == static_sites[i]);

    // 2. Static miss -> shifted scan fallback finds signature at +0x380..+0x482.
    memset(img + 0x200, 0x90, 0x400);
    for (int i = 0; i < 4; ++i) {{
        img[0x380 + deltas[i]] = 0x0f;
        img[0x380 + deltas[i] + 1] = 0xa2;
    }}
    reset_mock();
    assert(rclol_arm_group1_select(img, slide, static_sites) == 1);
    assert(g_site_count == 4 && armed_total == 4);
    for (int i = 0; i < 4; ++i)
        assert(armed_cookie[i] == 0x100000380UL + deltas[i]);

    // 3. Exact 0x104-byte section boundary is accepted and scanned.
    uint8_t exact_img[0x400];
    init_image(exact_img, sizeof(exact_img), 0x100000000ULL, 0x400ULL, 0x100000100ULL, 0x104ULL);
    for (int i = 0; i < 4; ++i) {{
        exact_img[0x100 + deltas[i]] = 0x0f;
        exact_img[0x100 + deltas[i] + 1] = 0xa2;
    }}
    reset_mock();
    assert(rclol_arm_group1_select(exact_img, (unsigned long) exact_img - 0x100000000UL, static_sites) == 1);
    assert(g_site_count == 4);

    // 4. Partial arm failure on site 2 rolls back fds 10 and 11 and resets g_site_count to 0.
    reset_mock();
    fail_on_call = 2;
    assert(rclol_arm_group1_select(img, slide, static_sites) == 0);
    assert(g_site_count == 0);
    assert(closed_count == 2 && closed_fds[0] == 10 && closed_fds[1] == 11);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            src_path = Path(folder) / 'cpuid_fallback_test.c'
            exe_path = Path(folder) / 'cpuid_fallback_test'
            src_path.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', str(src_path), '-o', str(exe_path)], check=True)
            subprocess.run([str(exe_path)], check=True)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_compat_cpuid_leaf_encodings_and_log_gating(self):
        src = cpuid_trace_source()
        log_start = src.index('static int rclol_open_log(')
        log_end = src.index('static void rclol_write_str(')
        log_helpers = src[log_start:log_end]

        spoof_start = src.index('static uint32_t rclol_brand_word(')
        spoof_end = src.index('// ---------------------------------------------------------------------------\n// Signal-handler fast path')
        spoof_helpers = src[spoof_start:spoof_end]

        # Confirm diagnostic logging stays off unless the flag file is opened.
        self.assertIn('static int g_log_enabled = 0;', src)
        self.assertIn('if (ffd >= 0) {\n\t\tg_log_enabled = 1;', src)

        harness = f"""
#include <assert.h>
#include <stdint.h>

#define RCLOL_CPUID_LOG_PATH "/tmp/should-not-open"
#define RCLOL_O_WRONLY 0x001
#define RCLOL_O_CREAT  0x040
#define RCLOL_O_APPEND 0x400
#define __NR_openat    257
#define __NR_write     1

struct darling_cpu_identity {{
    const char* brand;
    uint32_t brand_len;
    uint32_t signature;
    uint32_t leaf1_edx;
    uint32_t leaf1_ecx;
    uint32_t leaf1_ecx_system;
    uint32_t cores;
    uint32_t threads;
    uint32_t max_basic;
    uint32_t max_ext;
    uint32_t phys_bits;
    uint32_t virt_bits;
    uint32_t l1d_kb;
    uint32_t l2_kb;
    uint32_t l3_kb;
    uint32_t base_mhz;
    uint32_t max_mhz;
}};

static const struct darling_cpu_identity k_id = {{
    .brand = "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz",
    .brand_len = 40,
    .signature = 0x000906eau,
    .leaf1_edx = 0xbfebfbffu,
    .leaf1_ecx = 0x7ffafbbfu,
    .leaf1_ecx_system = 0x0122c9b4u,
    .cores = 6,
    .threads = 12,
    .max_basic = 0x16u,
    .max_ext = 0x80000008u,
    .phys_bits = 39,
    .virt_bits = 48,
    .l1d_kb = 32,
    .l2_kb = 256,
    .l3_kb = 12288,
    .base_mhz = 2600,
    .max_mhz = 4500,
}};

static const struct darling_cpu_identity* rclol_identity(void) {{ return &k_id; }}

static int g_log_enabled = 0;
static int g_log_fd = -1;
static int openat_calls = 0;

static long rclol_sys(long nr, long a1, long a2, long a3, long a4, long a5, long a6) {{
    (void)a1; (void)a2; (void)a3; (void)a4; (void)a5; (void)a6;
    if (nr == __NR_openat) {{
        openat_calls++;
        return 42;
    }}
    return 0;
}}

{log_helpers}
{spoof_helpers}

int main(void) {{
    // Without the diagnostic flag file, logging must never call openat().
    assert(rclol_open_log() == -1);
    rclol_write("hello", 5);
    assert(openat_calls == 0);
    assert(g_log_fd == -1);

    // Enabling diagnostics opens the log lazily once.
    g_log_enabled = 1;
    assert(rclol_open_log() == 42);
    assert(openat_calls == 1);
    assert(rclol_open_log() == 42);
    assert(openat_calls == 1);

    // Leaf 1: Intel 1-based max addressable logical ID count (16 for 6c/12t), CLFLUSH 8.
    uint32_t a = 0, b = 0x05000000u, c = 0x7ffafbbfu, d = 0;
    rclol_cpuid_trace_spoof(1u, 0u, &a, &b, &c, &d);
    assert(a == 0x000906eau);
    assert(((b >> 24) & 0xffu) == 0x05u);
    assert(((b >> 16) & 0xffu) == 16u);
    assert(((b >> 8) & 0xffu) == 8u);
    assert((b & 0xffu) == 0u);
    assert(d == 0xbfebfbffu);
    assert(c == 0x7ffafbbfu);

    // Leaf 1 ECX on a Zen host (issue #31): the AMD word lacks VMX, EST, TM2,
    // PCID, x2APIC... that every Coffee Lake reports; the result is exactly a
    // real i7-9750H's word.
    a = 0; b = 0; c = 0x7ed8320bu; d = 0x178bfbffu;
    rclol_cpuid_trace_spoof(1u, 0u, &a, &b, &c, &d);
    assert(c == 0x7ffafbbfu);
    // A host without FMA/MOVBE/F16C/RDRAND is never offered them; the
    // hypervisor bit (31), SMX and DCA, which a 9750H never sets, are dropped.
    c = 0x80000000u | 0x00040040u | (0x7ffafbbfu & ~((1u << 12) | (1u << 22) | (1u << 29) | (1u << 30)));
    rclol_cpuid_trace_spoof(1u, 0u, &a, &b, &c, &d);
    assert(c == (0x7ffafbbfu & ~((1u << 12) | (1u << 22) | (1u << 29) | (1u << 30))));

    // A host APIC ID beyond the identity's 12 threads (a 32-thread or
    // multi-CCD host) folds below the 16 IDs leaf 1 advertises, keeping SMT parity.
    for (uint32_t host = 0; host < 256u; ++host) {{
        a = 0; b = host << 24; c = 0; d = 0;
        rclol_cpuid_trace_spoof(1u, 0u, &a, &b, &c, &d);
        assert((b >> 24) < 12u);
        assert(((b >> 24) & 1u) == (host & 1u));
    }}

    // Leaf 2: Coffee Lake descriptors, 0xff defers caches to leaf 4.
    rclol_cpuid_trace_spoof(2u, 0u, &a, &b, &c, &d);
    assert(a == 0x76036301u && b == 0x00f0b5ffu && c == 0u && d == 0x00c30000u);

    // Leaf 4: the i7-9750H's real deterministic cache parameters.
    static const uint32_t leaf4[5][4] = {{
        {{ 0x1c004121u, 0x01c0003fu, 0x0000003fu, 0u }},
        {{ 0x1c004122u, 0x01c0003fu, 0x0000003fu, 0u }},
        {{ 0x1c004143u, 0x00c0003fu, 0x000003ffu, 0u }},
        {{ 0x1c03c163u, 0x03c0003fu, 0x00002fffu, 6u }},
        {{ 0u, 0u, 0u, 0u }},
    }};
    for (uint32_t i = 0; i < 5u; ++i) {{
        a = b = c = d = 0xdeadbeefu;
        rclol_cpuid_trace_spoof(4u, i, &a, &b, &c, &d);
        assert(a == leaf4[i][0] && b == leaf4[i][1] && c == leaf4[i][2] && d == leaf4[i][3]);
    }}

    // Leaf 0xb: 2 threads per core, 12 per package, x2APIC ID folded like leaf 1.
    a = b = c = 0xdeadbeefu; d = 29u;
    rclol_cpuid_trace_spoof(0xbu, 0u, &a, &b, &c, &d);
    assert(a == 1u && b == 2u && c == 0x100u && d == 5u);
    a = b = c = 0xdeadbeefu; d = 29u;
    rclol_cpuid_trace_spoof(0xbu, 1u, &a, &b, &c, &d);
    assert(a == 4u && b == 12u && c == 0x201u && d == 5u);
    a = b = c = 0xdeadbeefu; d = 29u;
    rclol_cpuid_trace_spoof(0xbu, 2u, &a, &b, &c, &d);
    assert(a == 0u && b == 0u && c == 2u && d == 5u);

    // Leaves 0x15 and 0x16: 24 MHz * 216/2 TSC, 2600/4500/100 MHz.
    rclol_cpuid_trace_spoof(0x15u, 0u, &a, &b, &c, &d);
    assert(a == 2u && b == 216u && c == 0u && d == 0u);
    rclol_cpuid_trace_spoof(0x16u, 0u, &a, &b, &c, &d);
    assert(a == 2600u && b == 4500u && c == 100u && d == 0u);

    // Above the maximum basic or extended leaf: the highest basic leaf, as on
    // Intel; the host's AMD topology leaves never show through.
    static const uint32_t beyond[] = {{ 0x17u, 0x1fu, 0x3fffffffu, 0x80000009u, 0x8000001du, 0x8000001eu, 0xffffffffu }};
    for (uint32_t i = 0; i < sizeof(beyond) / sizeof(beyond[0]); ++i) {{
        a = b = c = d = 0xdeadbeefu;
        rclol_cpuid_trace_spoof(beyond[i], 0u, &a, &b, &c, &d);
        assert(a == 2600u && b == 4500u && c == 100u && d == 0u);
    }}
    // The hypervisor range and the feature leaves pass through.
    a = b = c = d = 0x12345678u;
    rclol_cpuid_trace_spoof(0x40000000u, 0u, &a, &b, &c, &d);
    assert(a == 0x12345678u && b == 0x12345678u && c == 0x12345678u && d == 0x12345678u);
    rclol_cpuid_trace_spoof(7u, 0u, &a, &b, &c, &d);
    assert(a == 0x12345678u && b == 0x12345678u && c == 0x12345678u && d == 0x12345678u);

    // Leaf 0x80000001: AMD extended signature and package type cleared;
    // 0x80000005 (AMD L1 caches) is reserved on Intel.
    a = 0x00a20f12u; b = 0x40000000u; c = 0xffffffffu; d = 0xffffffffu;
    rclol_cpuid_trace_spoof(0x80000001u, 0u, &a, &b, &c, &d);
    assert(a == 0u && b == 0u && (c & 0x00003fc4u) == 0u && (d & 0xc0400000u) == 0u);
    a = b = c = d = 0xdeadbeefu;
    rclol_cpuid_trace_spoof(0x80000005u, 0u, &a, &b, &c, &d);
    assert(a == 0u && b == 0u && c == 0u && d == 0u);

    // Leaf 0x80000006: Intel L2 256 KiB, 4-way (0x4), 64-byte line (0x40); EAX/EBX/EDX zero.
    a = 0xdeadbeefu; b = 0xdeadbeefu; c = 0xdeadbeefu; d = 0xdeadbeefu;
    rclol_cpuid_trace_spoof(0x80000006u, 0u, &a, &b, &c, &d);
    assert(a == 0u && b == 0u && d == 0u);
    assert(c == ((256u << 16) | 0x4040u));

    // Leaf 0x80000008: Intel 39/48 address bits; EBX/ECX/EDX zero (no AMD feature/core bits).
    a = 0xdeadbeefu; b = 0xdeadbeefu; c = 0xdeadbeefu; d = 0xdeadbeefu;
    rclol_cpuid_trace_spoof(0x80000008u, 0u, &a, &b, &c, &d);
    assert(a == 0x3027u);
    assert(b == 0u && c == 0u && d == 0u);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            src_path = Path(folder) / 'cpuid_spoof_test.c'
            exe_path = Path(folder) / 'cpuid_spoof_test'
            src_path.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(src_path), '-o', str(exe_path)], check=True)
            subprocess.run([str(exe_path)], check=True)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_machdep_features_report_the_same_leaf1_words(self):
        patch = (ROOT / 'patches/xnu.patch').read_text()

        def added(path):
            marker = f'diff --git a/{path} b/{path}\n'
            section = patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]
            return '\n'.join(line[1:] for line in section.splitlines()
                             if line.startswith('+') and not line.startswith('+++'))

        base = 'darling/src/libsystem_kernel/emulation/'
        sysctl = added(base + 'src/xnu_syscall/bsd/helper/misc/sysctl_machdep.c')
        identity = added(base + 'src/common/cpu_identity.c')
        expression = '(ecx & id->leaf1_ecx) | (id->leaf1_ecx & id->leaf1_ecx_system)'
        # The game's leaf 1 and machdep.cpu.features cannot disagree.
        self.assertIn('*ecx = ' + expression.replace('(ecx', '(*ecx', 1), cpuid_trace_source())
        self.assertIn('ecx = ' + expression, sysctl)
        self.assertIn('.leaf1_ecx = 0x7ffafbbfu,', identity)
        self.assertIn('.leaf1_ecx_system = 0x0122c9b4u,', identity)
        # macOS names (xnu feature_map), ECX included.
        for name in ('"PSE36"', '"PCLMULQDQ"', '"MON"', '"DSCPL"', '"TPR"', '"SEGLIM64"',
                     '"TSCTMR"', '"AVX1.0"'):
            self.assertIn(name, sysctl)
        self.assertNotIn('"PSE-36"', sysctl)
        self.assertNotIn('"IA64"', sysctl)

    def test_log_uuid_formats_hex_without_width_specifiers(self):
        src = cpuid_trace_source()
        start = src.index('static void rclol_log_uuid(')
        end = src.index('// ---------------------------------------------------------------------------\n// Config parsing')
        fn = src[start:end]
        self.assertNotIn('%02x', fn)

        harness = f"""
#include <assert.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static char captured[256];
static int write_calls = 0;

static void rclol_write_str(const char* s) {{
    write_calls++;
    strncpy(captured, s, sizeof(captured) - 1);
    captured[sizeof(captured) - 1] = '\\0';
}}

static int __simple_snprintf(char* buf, size_t size, const char* fmt, ...) {{
    // Darling's __simple_snprintf does not support width/zero-padding specifiers
    // like %02x; reject them here so the regression cannot silently return.
    assert(strstr(fmt, "%0") == NULL);
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, size, fmt, ap);
    va_end(ap);
    return n;
}}

{fn}

int main(void) {{
    uint8_t img[128];
    memset(img, 0, sizeof(img));
    uint32_t* mh = (uint32_t*) img;
    mh[0] = 0xfeedfacfU;
    mh[4] = 1;
    mh[5] = 24;

    uint8_t* cmd = img + 32;
    *(uint32_t*) (cmd + 0) = 0x1bU;
    *(uint32_t*) (cmd + 4) = 24U;
    const uint8_t uuid[16] = {{
        0x01, 0x23, 0x45, 0x67, 0x08, 0x09, 0x0a, 0x0b,
        0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0xaa, 0xbc, 0xff
    }};
    memcpy(cmd + 8, uuid, 16);

    rclol_log_uuid(img);
    assert(write_calls == 1);
    char expected[256];
    snprintf(expected, sizeof(expected),
             "cpuid-trace: image base=0x%lx uuid=01234567-0809-0a0b-0c0d-0e0f10aabcff\\n",
             (unsigned long) img);
    assert(strcmp(captured, expected) == 0);

    // Truncated LC_UUID load command must be skipped without reading out of bounds.
    *(uint32_t*) (cmd + 4) = 16U;
    write_calls = 0;
    rclol_log_uuid(img);
    assert(write_calls == 0);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            src_path = Path(folder) / 'cpuid_uuid_test.c'
            exe_path = Path(folder) / 'cpuid_uuid_test'
            src_path.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', str(src_path), '-o', str(exe_path)], check=True)
            subprocess.run([str(exe_path)], check=True)


if __name__ == '__main__':
    unittest.main()
