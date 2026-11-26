"""machdep.cpu leaves decoded from cpuid, checked against a real i7-9750H.

max_ext, extmodel, extfamily, signature, brand, the feature words and their
name strings, the per-package counts and the cache, address_bits and tsc_ccc
nodes used to fail with ENOENT. Under the compat profile they decode the cpuid
trap's own view, so this test feeds a host's raw cpuid through the patched
rclol_cpuid_trace_spoof() and compares with `sysctl machdep.cpu` from a
MacBook Pro with that CPU (macOS 10.15.7, moby/moby issue 42822).
"""
from pathlib import Path
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
#include <stdint.h>
#include <string.h>
{header}
{identity}
static const struct darling_cpu_identity* rclol_identity(void) {{ return &g_compat_identity; }}
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
static int g_old_host = 0;      /* maximum basic leaf 4 */
static int g_invariant_tsc = 1;
static int g_topoext = 1;       /* AMD leaf 0x8000001E */

/* The raw cpuid of the host: a Zen 3 (leaf 7 EBX set to the bits it shares
 * with the i7-9750H plus AVX-512 and SHA, to exercise the mask), or the
 * i7-9750H itself. */
static struct cpuid_regs host_cpuid(unsigned int leaf, unsigned int subleaf)
{{
    struct cpuid_regs r = {{ 0, 0, 0, 0 }};
    switch (leaf) {{
    case 0: r.eax = g_old_host ? 4 : g_amd ? 0x10 : 0x16; break;
    case 1:
        r.eax = g_amd ? 0x00a20f12 : 0x000906ea;
        r.ebx = g_amd ? 0x050c0800 : 0x05100800;
        r.ecx = g_amd ? 0x7ed8320b : 0x7ffafbbf;
        r.edx = g_amd ? 0x178bfbff : 0xbfebfbff;
        break;
    case 4:
        if (!g_amd && subleaf < 4) {{
            static const unsigned int a[] = {{ 0x1c004121, 0x1c004122, 0x1c004143, 0x1c03c163 }};
            static const unsigned int b[] = {{ 0x01c0003f, 0x01c0003f, 0x00c0003f, 0x03c0003f }};
            r.eax = a[subleaf]; r.ebx = b[subleaf];
        }}
        break;
    case 7:
        if (g_old_host) {{
            /* above the maximum leaf: the data of leaf 4, EBX bit 5 set */
            r.eax = 0x1c004121; r.ebx = 0x01c0003f; r.ecx = 0x3f;
        }} else if (subleaf == 0) {{
            r.ebx = g_amd ? 0x219c07a9 | (1u << 16) | (1u << 29) : 0x029c67af;
            r.ecx = g_amd ? 0x0040069c : 0x40000000;
            r.edx = g_amd ? 0x00000010 : 0x9c002e00;
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
    /* leaf 7: the host's bits inside the Coffee Lake mask; no AVX-512, no SHA */
    leaf7 = quad(handle_leaf7_feature_bits);
    assert(leaf7 == (0x219c07a9u & DARLING_COMPAT_LEAF7_EBX));
    assert(strcmp(string(handle_leaf7_features),
        "RDWRFSGS BMI1 AVX2 SMEP BMI2 ERMS INVPCID RDSEED ADX SMAP CLFSOPT") == 0);
    len = sizeof(word); old = &word;
    assert(CALL(handle_leaf7_feature_bits_edx) == -ENOENT); /* the trap clears EDX */
    /* leaf 0x80000001 as the trap presents it, plus the invariant TSC */
    {{
        struct cpuid_regs ext = presented_cpuid(0x80000001u, 0);
        assert(quad(handle_extfeature_bits) == (((unsigned long long) ext.ecx << 32) | ext.edx | 0x100));
    }}
    assert(strcmp(string(handle_extfeatures), "SYSCALL XD 1GBPAGE EM64T LAHF LZCNT RDTSCP TSCI") == 0);

    /* compat on the part itself */
    g_amd = 0;
    identity_leaves();
    assert(quad(handle_leaf7_feature_bits) == (0x029c67afu & DARLING_COMPAT_LEAF7_EBX));
    assert(strcmp(string(handle_leaf7_features),
        "RDWRFSGS TSC_THREAD_OFFSET SGX BMI1 AVX2 SMEP BMI2 ERMS INVPCID FPU_CSDS MPX "
        "RDSEED ADX SMAP CLFSOPT IPT") == 0);

    /* host profile on the part: XNU's decoding reproduces the real dump */
    g_compat = 0;
    identity_leaves();
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
    g_compat = 0; g_old_host = 0;

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
