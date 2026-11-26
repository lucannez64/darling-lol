"""machdep.cpu leaves decoded from cpuid, checked against a real i7-9750H.

max_ext, extmodel, extfamily, signature, brand, the feature words and their
name strings, the per-package counts and the cache, address_bits and tsc_ccc
nodes used to fail with ENOENT. Under the compat profile they decode the cpuid
trap's own view, so this test feeds a host's raw cpuid through the patched
rclol_cpuid_trace_spoof() and compares with `sysctl machdep.cpu` from a
MacBook Pro with that CPU (macOS 10.15.7, moby/moby issue 42822).

The mwait, thermal, xsave, arch_perf and tlb nodes, microcode_version and
processor_flag are checked against the same dump. The raw words of leaves 5, 6
and 0xa beyond the fields XNU decodes are those of an i7-9750H in the public
InstLatx64 cpuid dumps (GenuineIntel00906EA_CoffeelakeH_CPUID2), which agree
with the Mac on every decoded field; the Zen 3 host words are those of
AuthenticAMD0A20F12_K19_Vermeer_00_CPUID. machdep.tsc.frequency is XNU's
24 MHz crystal times the 216/2 ratio of leaf 0x15, as on the Coffee Lake Macs
whose dumps include that node.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EMULATION = 'darling/src/libsystem_kernel/emulation'
MACHDEP = f'{EMULATION}/src/xnu_syscall/bsd/helper/misc/sysctl_machdep.c'
TRACE = f'{EMULATION}/src/linux_premigration/signal/cpuid_trace.c'

LEAVES = {
    'max_ext': 'CTLTYPE_INT', 'extmodel': 'CTLTYPE_INT', 'extfamily': 'CTLTYPE_INT',
    'feature_bits': 'CTLTYPE_QUAD', 'leaf7_feature_bits': 'CTLTYPE_INT',
    'leaf7_feature_bits_edx': 'CTLTYPE_INT', 'extfeature_bits': 'CTLTYPE_QUAD',
    'signature': 'CTLTYPE_INT', 'brand': 'CTLTYPE_INT',
    'leaf7_features': 'CTLTYPE_STRING', 'extfeatures': 'CTLTYPE_STRING',
    'logical_per_package': 'CTLTYPE_INT', 'cores_per_package': 'CTLTYPE_INT',
    'cache': 'CTLTYPE_NODE', 'address_bits': 'CTLTYPE_NODE', 'tsc_ccc': 'CTLTYPE_NODE',
    'linesize': 'CTLTYPE_INT', 'L2_associativity': 'CTLTYPE_INT', 'size': 'CTLTYPE_INT',
    'physical': 'CTLTYPE_INT', 'virtual': 'CTLTYPE_INT',
    'numerator': 'CTLTYPE_INT', 'denominator': 'CTLTYPE_INT',
    'microcode_version': 'CTLTYPE_INT', 'processor_flag': 'CTLTYPE_INT',
    'mwait': 'CTLTYPE_NODE', 'thermal': 'CTLTYPE_NODE', 'xsave': 'CTLTYPE_NODE',
    'arch_perf': 'CTLTYPE_NODE', 'tlb': 'CTLTYPE_NODE', 'inst': 'CTLTYPE_NODE',
    'data': 'CTLTYPE_NODE', 'tsc': 'CTLTYPE_NODE', 'frequency': 'CTLTYPE_QUAD',
    'linesize_min': 'CTLTYPE_INT', 'linesize_max': 'CTLTYPE_INT',
    'extensions': 'CTLTYPE_INT', 'sub_Cstates': 'CTLTYPE_INT',
    'sensor': 'CTLTYPE_INT', 'dynamic_acceleration': 'CTLTYPE_INT',
    'invariant_APIC_timer': 'CTLTYPE_INT', 'thresholds': 'CTLTYPE_INT',
    'ACNT_MCNT': 'CTLTYPE_INT', 'core_power_limits': 'CTLTYPE_INT',
    'fine_grain_clock_mod': 'CTLTYPE_INT', 'package_thermal_intr': 'CTLTYPE_INT',
    'hardware_feedback': 'CTLTYPE_INT', 'energy_policy': 'CTLTYPE_INT',
    'extended_state': 'CTLTYPE_INT', 'extended_state1': 'CTLTYPE_INT',
    'version': 'CTLTYPE_INT', 'number': 'CTLTYPE_INT', 'width': 'CTLTYPE_INT',
    'events_number': 'CTLTYPE_INT', 'events': 'CTLTYPE_INT',
    'fixed_number': 'CTLTYPE_INT', 'fixed_width': 'CTLTYPE_INT',
    'small': 'CTLTYPE_INT', 'large': 'CTLTYPE_INT', 'small_level1': 'CTLTYPE_INT',
    'large_level1': 'CTLTYPE_INT', 'shared': 'CTLTYPE_INT',
}

# The children of each node, in the order `sysctl machdep.cpu` prints them on
# the MacBook Pro (tlb: every leaf XNU registers).
NODES = {
    'mwait': ['linesize_min', 'linesize_max', 'extensions', 'sub_Cstates'],
    'thermal': ['sensor', 'dynamic_acceleration', 'invariant_APIC_timer', 'thresholds',
                'ACNT_MCNT', 'core_power_limits', 'fine_grain_clock_mod',
                'package_thermal_intr', 'hardware_feedback', 'energy_policy'],
    'xsave': ['extended_state', 'extended_state1'],
    'arch_perf': ['version', 'number', 'width', 'events_number', 'events',
                  'fixed_number', 'fixed_width'],
    'tlb_inst': ['small', 'large'],
    'tlb_data': ['small', 'small_level1', 'large', 'large_level1'],
    'tlb': ['inst', 'data', 'shared'],
}


def added_lines(path):
    patch = (ROOT / 'patches/xnu.patch').read_text()
    section = patch.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def patched_lines(path):
    """Added and context lines, for code that keeps upstream braces."""
    patch = (ROOT / 'patches/xnu.patch').read_text()
    section = patch.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith(('+', ' ')) and not line.startswith('+++'))


def function(source, signature):
    source += '\n'
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


class MachdepCpuLeavesTests(unittest.TestCase):
    def test_leaves_are_registered(self):
        machdep = added_lines(MACHDEP)
        for name, kind in LEAVES.items():
            self.assertRegex(machdep, rf'\.type = {kind}, [^\n]*\.name = "{name}", ', name)

    def test_groups_hold_the_leaves_of_a_real_mac(self):
        machdep = added_lines(MACHDEP)
        for node, names in NODES.items():
            table = machdep.split(f'sysctls_machdep_cpu_{node}[] = {{\n', 1)[1].split('};', 1)[0]
            self.assertEqual(re.findall(r'\.name = "(\w+)"', table), names, node)
        # XNU's types: extended_state is four unsigned words, tsc.frequency a quad
        for name in NODES['xsave']:
            self.assertIn(f'.type = CTLTYPE_INT, .exttype = "IU", .name = "{name}", ', machdep)
        self.assertIn('{ .oid = 1, .type = CTLTYPE_QUAD, .exttype = "Q", .name = "frequency", ', machdep)
        self.assertIn('.name = "tsc", .subctls = sysctls_machdep_tsc }', machdep)

    def test_table_oids_and_names_are_unique(self):
        machdep = patched_lines(MACHDEP)
        # The named OIDs are enumerators the compiler numbers in sequence: no
        # added one sets its own value, so distinct names are distinct numbers.
        self.assertEqual(re.findall(r'^\t(_\w+) = ', added_lines(MACHDEP), re.M), [])
        tables = re.findall(r'known_sysctl (\w+)\[\] = \{\n(.*?)\n\};', machdep, re.S)
        self.assertGreaterEqual(len(tables), 13)
        for table, body in tables:
            found = re.findall(r'\.oid = (\w+), [^\n]*\.name = "(\w+)"', body)
            self.assertEqual(len(found) + 1, body.count('.oid = '), table)
            oids = [oid for oid, _ in found]
            names = [name for _, name in found]
            self.assertEqual(len(oids), len(set(oids)), table)
            self.assertEqual(len(names), len(set(names)), table)

    def test_name_to_oid_takes_the_deepest_names(self):
        # machdep.cpu.tlb.inst.small has five components
        unspec = added_lines(f'{EMULATION}/src/xnu_syscall/bsd/helper/misc/sysctl_unspec.c')
        self.assertIn('int oid[CTL_MAXNAME];', unspec)
        self.assertIn('if (next == NULL || oid_len == CTL_MAXNAME)', unspec)

    def test_core_count_follows_the_fixed_count_opt_out(self):
        body = function(patched_lines(MACHDEP), 'sysctl_handler(handle_core_count) {')
        self.assertIn('\tif (topo->fixed)\n\t\tcore_count = DARLING_CPU_FIXED_COUNT;\n\n'
                      '\tif (core_count == 0) {', body)

    def test_compat_values_come_from_the_cpuid_trap_view(self):
        machdep = added_lines(MACHDEP)
        body = function(machdep, 'static struct cpuid_regs presented_cpuid(')
        self.assertIn('if (darling_compat_profile_enabled())\n'
                      '\t\trclol_cpuid_trace_spoof(leaf, subleaf, &r.eax, &r.ebx, &r.ecx, &r.edx);', body)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_compat_leaves_match_a_real_i7_9750h(self):
        header = added_lines(f'{EMULATION}/include/common/cpu_identity.h')
        table_source = added_lines(f'{EMULATION}/src/common/cpu_identity.c')
        start = table_source.index('#define DARLING_COMPAT_BRAND')
        identity = table_source[start:table_source.index('};', start) + 2]
        trace = added_lines(TRACE)
        spoof = trace[trace.index('static uint32_t rclol_brand_word('):
                      trace.index('// ---------------------------------------------------------------------------\n'
                                  '// Signal-handler fast path')]
        machdep = added_lines(MACHDEP)
        copyout_int32 = function(patched_lines(MACHDEP), 'static int copyout_int32(')
        patched = patched_lines(MACHDEP)
        handlers = patched[patched.index('static unsigned int max_basic_leaf(void)'):]

        harness = f"""
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
{header}
{identity}
static const struct darling_cpu_identity* rclol_identity(void) {{ return &g_compat_identity; }}
static void rclol_host_cpuid(uint32_t leaf, uint32_t subleaf,
    uint32_t* eax, uint32_t* ebx, uint32_t* ecx, uint32_t* edx);
{spoof}
#define sysctl_handler(_hname) long _hname(const int* name, int nlen, void* old, \\
    unsigned long* oldlen, void* _new, unsigned long newlen)
static void copyout_string(const char* str, char* out, unsigned long* out_len)
{{
    if (out) strcpy(out, str);
    *out_len = strlen(str) + 1;
}}
{copyout_int32}

struct cpuid_regs {{ unsigned int eax, ebx, ecx, edx; }};
static int g_compat = 1;
static int g_amd = 1;
static int g_old_host = 0;      /* maximum basic leaf g_old_max */
static unsigned int g_old_max = 4;
static int g_leaf2_set = 0;     /* leaf 2 is g_leaf2 */
static unsigned int g_leaf2[4];
static int g_cpuinfo = 1;       /* the host has a /proc/cpuinfo */
static int g_invariant_tsc = 1;
static int g_topoext = 1;       /* AMD leaf 0x8000001E */
static int g_monitor = 1;       /* leaf 1 ECX bit 3 */
static int g_systemroot = 1;    /* the host root is mounted in the guest */

/* The raw cpuid of the host: a Zen 3 (leaf 7 EBX set to the bits it shares
 * with the i7-9750H plus AVX-512 and SHA, to exercise the mask), or the
 * i7-9750H itself. */
static struct cpuid_regs host_cpuid(unsigned int leaf, unsigned int subleaf)
{{
    struct cpuid_regs r = {{ 0, 0, 0, 0 }};
    if (g_old_host && leaf > g_old_max && leaf < 0x80000000u) {{
        /* above the maximum leaf: the data of leaf 4, EBX bit 5 set */
        r.eax = 0x1c004121; r.ebx = 0x01c0003f; r.ecx = 0x3f;
        return r;
    }}
    switch (leaf) {{
    case 0: r.eax = g_old_host ? g_old_max : g_amd ? 0x10 : 0x16; break;
    case 1:
        r.eax = g_amd ? 0x00a20f12 : 0x000906ea;
        r.ebx = g_amd ? 0x050c0800 : 0x05100800;
        r.ecx = (g_amd ? 0x7ed8320b : 0x7ffafbbf) & (g_monitor ? ~0u : ~8u);
        r.edx = g_amd ? 0x178bfbff : 0xbfebfbff;
        break;
    case 2:
        if (g_leaf2_set) {{
            r.eax = g_leaf2[0]; r.ebx = g_leaf2[1]; r.ecx = g_leaf2[2]; r.edx = g_leaf2[3];
        }} else if (!g_amd) {{
            r.eax = 0x76036301; r.ebx = 0x00f0b5ff; r.edx = 0x00c30000;
        }}
        break;
    case 4:
        if (!g_amd && subleaf < 4) {{
            static const unsigned int a[] = {{ 0x1c004121, 0x1c004122, 0x1c004143, 0x1c03c163 }};
            static const unsigned int b[] = {{ 0x01c0003f, 0x01c0003f, 0x00c0003f, 0x03c0003f }};
            r.eax = a[subleaf]; r.ebx = b[subleaf];
        }}
        break;
    case 5:
        r.eax = 0x40; r.ebx = 0x40; r.ecx = 3; r.edx = g_amd ? 0x11 : 0x11142120;
        break;
    case 6:
        r.eax = g_amd ? 0x4 : 0x27f7; r.ebx = g_amd ? 0 : 2; r.ecx = g_amd ? 1 : 9;
        break;
    case 7:
        if (subleaf == 0) {{
            r.ebx = g_amd ? 0x219c07a9 | (1u << 16) | (1u << 29) : 0x029c67af;
            r.ecx = g_amd ? 0x0040069c : 0x40000000;
            r.edx = g_amd ? 0x00000010 : 0x9c002e00;
        }}
        break;
    case 0xa: if (!g_amd) {{ r.eax = 0x07300404; r.edx = 0x603; }} break;
    case 0xd:
        /* the part with XCR0 = 7, as macOS sets it; the Zen 3 with PKRU and CET states */
        if (subleaf == 0) {{
            r.eax = g_amd ? 0x207 : 0x1f; r.ebx = 0x340; r.ecx = g_amd ? 0x988 : 0x440;
        }} else if (subleaf == 1) {{
            r.eax = 0xf; r.ebx = g_amd ? 0x350 : 0x340; r.ecx = g_amd ? 0x1800 : 0x100;
        }} else if (subleaf == 2) {{
            r.eax = 0x100; r.ebx = 0x240;
        }}
        break;
    case 0x15: if (!g_amd) {{ r.eax = 2; r.ebx = 216; }} break;
    case 0x80000000: r.eax = (g_amd && g_topoext) ? 0x80000023 : 0x80000008; break;
    case 0x80000001:
        r.eax = g_amd ? 0x00a20f12 : 0;
        r.ecx = g_amd ? 0x75c237ff : 0x00000121;
        r.edx = g_amd ? 0x2fd3fbff : 0x2c100800;
        break;
    case 0x80000006: r.ecx = g_amd ? 0x02006140 : 0x01004040; break;
    case 0x80000007: r.edx = (g_amd ? 0x00006799 : 0x00000100) & (g_invariant_tsc ? ~0u : ~0x100u); break;
    case 0x8000001e: if (g_amd && g_topoext) r.ebx = 0x00000100; break; /* 2 threads per core */
    case 0x80000008: r.eax = g_amd ? 0x00003030 : 0x00003027; r.ecx = g_amd ? 0x0000400b : 0; break;
    }}
    return r;
}}

static unsigned int host_max_basic_leaf(void) {{ return host_cpuid(0, 0).eax; }}

static void rclol_host_cpuid(uint32_t leaf, uint32_t subleaf,
    uint32_t* eax, uint32_t* ebx, uint32_t* ecx, uint32_t* edx)
{{
    struct cpuid_regs r = host_cpuid(leaf, subleaf);
    *eax = r.eax; *ebx = r.ebx; *ecx = r.ecx; *edx = r.edx;
}}

const struct darling_cpu_identity* darling_compat_cpu_identity(void) {{ return &g_compat_identity; }}
int darling_compat_profile_enabled(void) {{ return g_compat; }}

static int __simple_sprintf(char* buffer, const char* format, ...)
{{
    va_list args;
    va_start(args, format);
    int length = vsprintf(buffer, format, args);
    va_end(args);
    return length;
}}

/* /sys/devices/system/cpu/cpu0/microcode of the host: revision 0xea and
 * platform ID 5 on the part, as the MacBook Pro reports them. /proc/cpuinfo
 * of a 3.4 GHz Zen 3: bogomips is twice the TSC rate in MHz. */
static bool read_string(const char* path, char* dst, int maxlen)
{{
    static const char sys[] = "/sys/devices/system/cpu/cpu0/microcode/";
    const char* value = 0;

    if (strncmp(path, "/Volumes/SystemRoot", 19) == 0) {{
        if (!g_systemroot)
            return false;
        path += 19;
    }}
    if (strcmp(path, "/proc/cpuinfo") == 0) {{
        if (g_cpuinfo)
            value = "processor\\t: 0\\ncpu MHz\\t\\t: 4650.000\\nbogomips\\t: 6787.23\\n";
    }} else if (strncmp(path, sys, sizeof(sys) - 1) != 0)
        return false;
    else if (strcmp(path + sizeof(sys) - 1, "version") == 0)
        value = g_amd ? "0xa201205\\n" : "0xea\\n";
    else if (strcmp(path + sizeof(sys) - 1, "processor_flags") == 0)
        value = g_amd ? "0x0\\n" : "0x20\\n";
    if (value == 0 || (int) strlen(value) >= maxlen)
        return false;
    strcpy(dst, value);
    return true;
}}

static struct cpuid_regs presented_cpuid(unsigned int leaf, unsigned int subleaf)
{{
    struct cpuid_regs r = host_cpuid(leaf, subleaf);
    if (g_compat)
        rclol_cpuid_trace_spoof(leaf, subleaf, &r.eax, &r.ebx, &r.ecx, &r.edx);
    return r;
}}
{handlers}

#define CALL(handler) handler(0, 0, old, &len, 0, 0)

static unsigned int integer(sysctl_handler((*handler)))
{{
    unsigned int value = 0xdeadbeef;
    unsigned long len = sizeof(value);
    void* old = &value;
    assert(CALL(handler) == 0 && len == sizeof(value));
    return value;
}}

static unsigned long long quad(sysctl_handler((*handler)))
{{
    unsigned long long value = 0;
    unsigned long len = sizeof(value);
    void* old = &value;
    assert(CALL(handler) == 0 && len == sizeof(value));
    return value;
}}

static const char* string(sysctl_handler((*handler)))
{{
    static char value[512];
    unsigned long len = sizeof(value);
    void* old = value;
    assert(CALL(handler) == 0 && len == strlen(value) + 1);
    return value;
}}

static int missing(sysctl_handler((*handler)))
{{
    unsigned int value = 0xdeadbeef;
    unsigned long len = sizeof(value);
    void* old = &value;
    return CALL(handler) == -ENOENT && value == 0xdeadbeef;
}}

static int words(sysctl_handler((*handler)), unsigned int a, unsigned int b, unsigned int c, unsigned int d)
{{
    unsigned int value[4] = {{ 0, 0, 0, 0 }};
    unsigned long len = sizeof(value);
    void* old = value;
    assert(CALL(handler) == 0 && len == sizeof(value));
    return value[0] == a && value[1] == b && value[2] == c && value[3] == d;
}}

/* XNU's leaf 2 TLB descriptors (osfmk/i386/cpuid.c), by the sysctl leaf each
 * one fills: a descriptor for both page sizes counts as large */
enum {{ INST_SMALL, INST_LARGE, DATA_SMALL, DATA_SMALL1, DATA_LARGE, DATA_LARGE1, SHARED, TLB_LEAVES }};
static const struct {{ unsigned char value; int leaf; unsigned int entries; }} tlb_descriptors[] = {{
    {{ 0x01, INST_SMALL, 32 }}, {{ 0x02, INST_LARGE, 2 }}, {{ 0x03, DATA_SMALL, 64 }},
    {{ 0x04, DATA_LARGE, 8 }}, {{ 0x05, DATA_LARGE1, 32 }}, {{ 0x0b, INST_LARGE, 4 }},
    {{ 0x4f, INST_SMALL, 32 }}, {{ 0x50, INST_LARGE, 64 }}, {{ 0x51, INST_LARGE, 128 }},
    {{ 0x52, INST_LARGE, 256 }}, {{ 0x55, INST_LARGE, 7 }}, {{ 0x56, DATA_LARGE, 16 }},
    {{ 0x57, DATA_SMALL, 16 }}, {{ 0x59, DATA_SMALL, 16 }}, {{ 0x5a, DATA_LARGE, 32 }},
    {{ 0x5b, DATA_LARGE, 64 }}, {{ 0x5c, DATA_LARGE, 128 }}, {{ 0x5d, DATA_LARGE, 256 }},
    {{ 0x76, INST_LARGE, 8 }}, {{ 0xb0, INST_SMALL, 128 }}, {{ 0xb1, INST_LARGE, 8 }},
    {{ 0xb2, INST_SMALL, 64 }}, {{ 0xb3, DATA_SMALL, 128 }}, {{ 0xb4, DATA_SMALL1, 256 }},
    {{ 0xb5, DATA_SMALL1, 64 }}, {{ 0xb6, DATA_SMALL1, 128 }}, {{ 0xba, DATA_LARGE1, 64 }},
    {{ 0xc1, SHARED, 1024 }}, {{ 0xca, SHARED, 512 }},
}};

/* every descriptor alone in leaf 2 fills its leaf and no other */
static void tlb_table(void)
{{
    sysctl_handler((*handlers[TLB_LEAVES])) = {{
        handle_tlb_inst_small, handle_tlb_inst_large, handle_tlb_data_small,
        handle_tlb_data_small_level1, handle_tlb_data_large, handle_tlb_data_large_level1,
        handle_tlb_shared,
    }};

    g_leaf2_set = 1;
    for (unsigned int i = 0; i < sizeof(tlb_descriptors) / sizeof(tlb_descriptors[0]); i++) {{
        /* in EAX above the count byte, then in the middle of EDX */
        for (int place = 0; place < 2; place++) {{
            memset(g_leaf2, 0, sizeof(g_leaf2));
            g_leaf2[0] = 1;
            if (place == 0)
                g_leaf2[0] |= (unsigned int) tlb_descriptors[i].value << 8;
            else
                g_leaf2[3] = (unsigned int) tlb_descriptors[i].value << 16;
            for (int leaf = 0; leaf < TLB_LEAVES; leaf++) {{
                if (leaf == tlb_descriptors[i].leaf)
                    assert(integer(handlers[leaf]) == tlb_descriptors[i].entries);
                else
                    assert(missing(handlers[leaf]));
            }}
        }}
    }}
    /* the low byte of EAX is a count, not the data TLB descriptor 0x03 */
    memset(g_leaf2, 0, sizeof(g_leaf2));
    g_leaf2[0] = 0x03;
    assert(missing(handle_tlb_data_small));
    /* a register with bit 31 set holds no descriptor */
    g_leaf2[0] = 1; g_leaf2[1] = 0x80000300;
    assert(missing(handle_tlb_data_small));
    g_leaf2[1] = 0x00000300;
    assert(integer(handle_tlb_data_small) == 64);
    /* the last of two descriptors of one TLB wins, as in XNU's loop */
    g_leaf2[1] = 0x00b30300;
    assert(integer(handle_tlb_data_small) == 128);
    g_leaf2_set = 0;
}}

/* `sysctl machdep.cpu` of the MacBook Pro: the groups cpuid leaves 2, 5, 6 and
 * 0xa fill, and the TSC frequency of leaf 0x15 */
static void group_leaves(void)
{{
    assert(integer(handle_mwait_linesize_min) == 64);
    assert(integer(handle_mwait_linesize_max) == 64);
    assert(integer(handle_mwait_extensions) == 3);
    assert(integer(handle_mwait_sub_cstates) == 286531872);
    assert(integer(handle_thermal_sensor) == 1);
    assert(integer(handle_thermal_dynamic_acceleration) == 1);
    assert(integer(handle_thermal_invariant_apic_timer) == 1);
    assert(integer(handle_thermal_thresholds) == 2);
    assert(integer(handle_thermal_acnt_mcnt) == 1);
    assert(integer(handle_thermal_core_power_limits) == 1);
    assert(integer(handle_thermal_fine_grain_clock_mod) == 1);
    assert(integer(handle_thermal_package_thermal_intr) == 1);
    assert(integer(handle_thermal_hardware_feedback) == 0);
    assert(integer(handle_thermal_energy_policy) == 1);
    assert(integer(handle_arch_perf_version) == 4);
    assert(integer(handle_arch_perf_number) == 4);
    assert(integer(handle_arch_perf_width) == 48);
    assert(integer(handle_arch_perf_events_number) == 7);
    assert(integer(handle_arch_perf_events) == 0);
    assert(integer(handle_arch_perf_fixed_number) == 3);
    assert(integer(handle_arch_perf_fixed_width) == 48);
    /* the three TLB leaves the Mac lists; it has no other */
    assert(integer(handle_tlb_inst_large) == 8);
    assert(integer(handle_tlb_data_small) == 64);
    assert(integer(handle_tlb_data_small_level1) == 64);
    assert(missing(handle_tlb_inst_small) && missing(handle_tlb_data_large));
    assert(missing(handle_tlb_data_large_level1) && missing(handle_tlb_shared));
    assert(integer(handle_microcode_version) == 234);
    assert(integer(handle_processor_flag) == 5);
    assert(quad(handle_tsc_frequency) == 2592000000ull); /* 24 MHz * 216 / 2 */
}}

/* the values every profile and host must give for the identity part */
static void identity_leaves(void)
{{
    assert(integer(handle_max_ext) == 2147483656u);
    assert(integer(handle_signature) == 591594);
    assert(integer(handle_extmodel) == 9);
    assert(integer(handle_extfamily) == 0);
    assert(integer(handle_brand) == 0);
    assert(quad(handle_feature_bits) == 9221959987971750911ull);
    assert(integer(handle_logical_per_package) == 16);
    assert(integer(handle_cores_per_package) == 8);
    assert(integer(handle_cache_linesize) == 64);
    assert(integer(handle_cache_l2_associativity) == 4);
    assert(integer(handle_cache_size) == 256);
    assert(integer(handle_address_bits_physical) == 39);
    assert(integer(handle_address_bits_virtual) == 48);
    assert(integer(handle_tsc_ccc_numerator) == 216);
    assert(integer(handle_tsc_ccc_denominator) == 2);
}}

int main(void)
{{
    unsigned long len;
    void* old;
    unsigned int word;
    unsigned long long leaf7;

    /* compat on a Zen 3 host: the real MacBook Pro's values */
    g_amd = 1;
    identity_leaves();
    group_leaves();
    /* leaf 0xd: the host's state components and area sizes, which XSAVE
     * writes; subleaf 1 without the host's CET states */
    assert(words(handle_xsave_extended_state, 0x207, 0x340, 0x988, 0));
    assert(words(handle_xsave_extended_state1, 0xf, 0x350, 0, 0));
    /* MONITOR/MWAIT parameters only beside leaf 1's MONITOR bit */
    g_monitor = 0;
    assert(integer(handle_mwait_linesize_min) == 0 && integer(handle_mwait_sub_cstates) == 0);
    assert((quad(handle_feature_bits) >> 35 & 1) == 0);
    g_monitor = 1;
    /* leaf 7: the host's bits inside the Coffee Lake mask; no AVX-512, no SHA */
    leaf7 = quad(handle_leaf7_feature_bits);
    assert(leaf7 == 0x009c07a9u);
    assert(strcmp(string(handle_leaf7_features),
        "RDWRFSGS BMI1 AVX2 SMEP BMI2 ERMS INVPCID RDSEED ADX SMAP CLFSOPT") == 0);
    len = sizeof(word); old = &word;
    assert(CALL(handle_leaf7_feature_bits_edx) == -ENOENT); /* the trap clears EDX */
    /* leaf 0x80000001 and the invariant TSC: the real part's word, none of the
     * host's AMD-only bits */
    assert(quad(handle_extfeature_bits) == 1241984796928ull);
    assert(strcmp(string(handle_extfeatures),
        "SYSCALL XD 1GBPAGE EM64T LAHF LZCNT PREFETCHW RDTSCP TSCI") == 0);

    /* compat on the part itself */
    g_amd = 0;
    identity_leaves();
    group_leaves();
    assert(words(handle_xsave_extended_state, 31, 832, 1088, 0));
    assert(words(handle_xsave_extended_state1, 15, 832, 256, 0));
    assert(quad(handle_leaf7_feature_bits) == 43804591ull); /* the real EBX word */
    assert(strcmp(string(handle_leaf7_features),
        "RDWRFSGS TSC_THREAD_OFFSET SGX BMI1 AVX2 SMEP BMI2 ERMS INVPCID FPU_CSDS MPX "
        "RDSEED ADX SMAP CLFSOPT IPT") == 0);

    /* host profile on the part: XNU's decoding reproduces the real dump */
    g_compat = 0;
    identity_leaves();
    group_leaves();
    assert(words(handle_xsave_extended_state, 31, 832, 1088, 0));
    assert(words(handle_xsave_extended_state1, 15, 832, 256, 0));
    g_systemroot = 0; /* the host's /sys directly */
    assert(integer(handle_microcode_version) == 234 && integer(handle_processor_flag) == 5);
    g_systemroot = 1;
    tlb_table();
    assert(quad(handle_leaf7_feature_bits) == ((1073741824ull << 32) | 43804591ull));
    assert(integer(handle_leaf7_feature_bits_edx) == 2617257472u);
    assert(quad(handle_extfeature_bits) == 1241984796928ull);
    assert(strcmp(string(handle_leaf7_features),
        "RDWRFSGS TSC_THREAD_OFFSET SGX BMI1 AVX2 SMEP BMI2 ERMS INVPCID FPU_CSDS MPX "
        "RDSEED ADX SMAP CLFSOPT IPT SGXLC MDCLEAR TSXFA IBRS STIBP L1DF SSBD") == 0);
    assert(strcmp(string(handle_extfeatures),
        "SYSCALL XD 1GBPAGE EM64T LAHF LZCNT PREFETCHW RDTSCP TSCI") == 0);

    /* host profile on the Zen 3: its own cpuid */
    g_amd = 1;
    assert(integer(handle_max_ext) == 0x80000023u);
    assert(integer(handle_signature) == 0x00a20f12);
    assert(integer(handle_extmodel) == 2 && integer(handle_extfamily) == 0xa);
    assert(integer(handle_logical_per_package) == 12);
    assert(integer(handle_cores_per_package) == 6);  /* 12 logical / 2 threads per core */
    g_topoext = 0;
    assert(integer(handle_max_ext) == 0x80000008u);
    assert(integer(handle_cores_per_package) == 12); /* no leaf 0x8000001E: one thread per core */
    g_topoext = 1;
    /* AMD mirrors CX8 at bit 8 of leaf 0x80000001 EDX: TSCI only from leaf 0x80000007 */
    assert(strstr(string(handle_extfeatures), "TSCI") != 0);
    g_invariant_tsc = 0;
    assert((quad(handle_extfeature_bits) & 0x100) == 0);
    assert(strstr(string(handle_extfeatures), "TSCI") == 0);
    g_invariant_tsc = 1;
    assert(integer(handle_cache_size) == 512);
    assert(integer(handle_cache_l2_associativity) == 8); /* encoded 6 */
    assert(integer(handle_address_bits_physical) == 48);
    assert(integer(handle_tsc_ccc_numerator) == 0); /* no leaf 0x15 */
    /* no leaf 0x15: the rate the host kernel measured, half its bogomips */
    assert(quad(handle_tsc_frequency) == 3393615000ull);
    g_systemroot = 0;
    assert(quad(handle_tsc_frequency) == 3393615000ull);
    g_systemroot = 1; g_cpuinfo = 0;
    len = 8; old = &leaf7;
    assert(CALL(handle_tsc_frequency) == -ENOENT);
    g_cpuinfo = 1;
    assert(integer(handle_mwait_sub_cstates) == 0x11);
    assert(integer(handle_thermal_sensor) == 0 && integer(handle_thermal_invariant_apic_timer) == 1);
    assert(integer(handle_thermal_thresholds) == 0 && integer(handle_thermal_acnt_mcnt) == 1);
    assert(integer(handle_thermal_energy_policy) == 0);
    assert(integer(handle_arch_perf_version) == 0 && integer(handle_arch_perf_fixed_width) == 0);
    assert(words(handle_xsave_extended_state, 0x207, 0x340, 0x988, 0));
    assert(words(handle_xsave_extended_state1, 0xf, 0x350, 0x1800, 0));
    /* AMD has no leaf 2 descriptors, so no TLB leaf */
    assert(missing(handle_tlb_inst_large) && missing(handle_tlb_data_small));
    assert(missing(handle_tlb_data_small_level1) && missing(handle_tlb_shared));
    assert(integer(handle_microcode_version) == 0x0a201205);
    assert(integer(handle_processor_flag) == 0); /* AMD has no platform ID */

    /* a host whose maximum leaf is below 7 has no leaf 7, whatever the compat
     * view's maximum: cpuid(7) returns leaf 4's data there */
    g_compat = 1; g_old_host = 1;
    len = 8; old = &leaf7;
    assert(CALL(handle_leaf7_feature_bits) == -ENOENT);
    len = sizeof(word); old = &word;
    assert(CALL(handle_leaf7_feature_bits_edx) == -ENOENT);
    {{
        char names[64];
        len = sizeof(names); old = names;
        assert(CALL(handle_leaf7_features) == -ENOENT);
    }}
    /* nor a leaf 0xd: none of leaf 4's bits reach the XSAVE words */
    assert(words(handle_xsave_extended_state, 0, 0, 0, 0));
    assert(words(handle_xsave_extended_state1, 0, 0, 0, 0));
    /* the groups of the identity part do not depend on the host's leaves */
    group_leaves();
    /* the host profile on that host: XNU has no such nodes below their leaf */
    g_compat = 0;
    assert(missing(handle_mwait_linesize_min) && missing(handle_thermal_sensor));
    assert(missing(handle_arch_perf_version));
    /* leaf 2 is below that maximum, so the part's TLB leaves are there; with
     * a maximum of 1, cpuid(2) returns leaf 1's data and none is decoded */
    g_amd = 0;
    assert(integer(handle_tlb_inst_large) == 8 && integer(handle_tlb_data_small) == 64);
    g_old_max = 1;
    assert(missing(handle_tlb_inst_small) && missing(handle_tlb_inst_large));
    assert(missing(handle_tlb_data_small) && missing(handle_tlb_shared));
    g_old_max = 4; g_amd = 1;
    {{
        unsigned int state[4];
        len = sizeof(state); old = state;
        assert(CALL(handle_xsave_extended_state) == -ENOENT);
        len = sizeof(state);
        assert(CALL(handle_xsave_extended_state1) == -ENOENT);
    }}
    g_old_host = 0;

    /* size queries and short buffers */
    len = 0; old = 0;
    assert(CALL(handle_feature_bits) == 0 && len == 8);
    len = 0;
    assert(CALL(handle_signature) == 0 && len == 4);
    len = 4; old = &word;
    word = 0x55555555;
    assert(CALL(handle_feature_bits) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 2;
    assert(CALL(handle_signature) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 4;
    /* the name strings too: a size query, then a short buffer is not truncated into */
    len = 0; old = 0;
    assert(CALL(handle_extfeatures) == 0 && len > 4);
    assert(CALL(handle_leaf7_features) == 0 && len > 4);
    word = 0x55555555; len = 4; old = &word;
    assert(CALL(handle_extfeatures) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 4;
    assert(CALL(handle_leaf7_features) == -ENOMEM && len == 0 && word == 0x55555555);
    /* the new leaves: 4 bytes, 16 for an XSAVE subleaf, 8 for the TSC frequency */
    g_compat = 1;
    len = 0; old = 0;
    assert(CALL(handle_thermal_thresholds) == 0 && len == 4);
    assert(CALL(handle_arch_perf_version) == 0 && len == 4);
    assert(CALL(handle_tlb_inst_large) == 0 && len == 4);
    assert(CALL(handle_microcode_version) == 0 && len == 4);
    assert(CALL(handle_processor_flag) == 0 && len == 4);
    assert(CALL(handle_xsave_extended_state) == 0 && len == 16);
    assert(CALL(handle_xsave_extended_state1) == 0 && len == 16);
    assert(CALL(handle_tsc_frequency) == 0 && len == 8);
    word = 0x55555555; old = &word;
    len = 3;
    assert(CALL(handle_mwait_sub_cstates) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 3;
    assert(CALL(handle_thermal_sensor) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 3;
    assert(CALL(handle_tlb_data_small) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 3;
    assert(CALL(handle_processor_flag) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 4;
    assert(CALL(handle_xsave_extended_state) == -ENOMEM && len == 0 && word == 0x55555555);
    len = 4;
    assert(CALL(handle_tsc_frequency) == -ENOMEM && len == 0 && word == 0x55555555);
    /* no length: an existence probe succeeds, a buffer without one is of
     * length zero */
    assert(handle_tsc_frequency(0, 0, 0, 0, 0, 0) == 0);
    assert(handle_xsave_extended_state(0, 0, 0, 0, 0, 0) == 0);
    assert(handle_feature_bits(0, 0, 0, 0, 0, 0) == 0);
    assert(handle_tsc_frequency(0, 0, &word, 0, 0, 0) == -ENOMEM && word == 0x55555555);
    assert(handle_xsave_extended_state1(0, 0, &word, 0, 0, 0) == -ENOMEM && word == 0x55555555);
    assert(handle_thermal_sensor(0, 0, 0, 0, 0, 0) == 0);
    assert(handle_thermal_sensor(0, 0, &word, 0, 0, 0) == -ENOMEM && word == 0x55555555);
    assert(handle_microcode_version(0, 0, &word, 0, 0, 0) == -ENOMEM && word == 0x55555555);
    assert(handle_tlb_data_small(0, 0, &word, 0, 0, 0) == -ENOMEM && word == 0x55555555);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'machdep.c'
            binary = Path(folder) / 'machdep'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                            '-Wno-unused-function', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
