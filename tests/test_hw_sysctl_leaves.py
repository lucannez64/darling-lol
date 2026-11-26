"""The hw.* leaves an Intel Mac answers besides the CPU counts and cache arrays.

hw.cpufamily, hw.byteorder, hw.busfrequency, hw.tbfrequency, hw.cachelinesize,
the L1 cache sizes, the legacy numeric OIDs and the rest of hw.optional used to
fail with ENOENT. These checks compile the patched handlers against the real
identity header and table, in both profiles.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EMULATION = 'darling/src/libsystem_kernel/emulation'
SYSCTL_HW = f'{EMULATION}/src/xnu_syscall/bsd/helper/misc/sysctl_hw.c'

# name -> (OID expression, type) as bsd/kern/kern_mib.c registers them on x86-64
LEAVES = {
    'byteorder': ('HW_BYTEORDER', 'CTLTYPE_INT'),
    'physmem': ('HW_PHYSMEM', 'CTLTYPE_INT'),
    'usermem': ('HW_USERMEM', 'CTLTYPE_INT'),
    'vectorunit': ('HW_VECTORUNIT', 'CTLTYPE_INT'),
    'epoch': ('HW_EPOCH', 'CTLTYPE_INT'),
    'targettype': ('_HW_TARGETTYPE', 'CTLTYPE_STRING'),
    'cpufamily': ('_HW_CPUFAMILY', 'CTLTYPE_INT'),
    'cpusubfamily': ('_HW_CPUSUBFAMILY', 'CTLTYPE_INT'),
    'pagesize32': ('_HW_PAGESIZE32', 'CTLTYPE_QUAD'),
    'busfrequency': ('_HW_BUSFREQUENCY', 'CTLTYPE_QUAD'),
    'busfrequency_min': ('_HW_BUSFREQUENCY_MIN', 'CTLTYPE_QUAD'),
    'busfrequency_max': ('_HW_BUSFREQUENCY_MAX', 'CTLTYPE_QUAD'),
    'busfrequency_compat': ('HW_BUS_FREQ', 'CTLTYPE_INT'),
    'cpufrequency_min': ('_HW_CPUFREQUENCY_MIN', 'CTLTYPE_QUAD'),
    'cpufrequency_max': ('_HW_CPUFREQUENCY_MAX', 'CTLTYPE_QUAD'),
    'cachelinesize': ('_HW_CACHELINESIZE', 'CTLTYPE_QUAD'),
    'cachelinesize_compat': ('HW_CACHELINE', 'CTLTYPE_INT'),
    'l1icachesize': ('_HW_L1ICACHESIZE', 'CTLTYPE_QUAD'),
    'l1icachesize_compat': ('HW_L1ICACHESIZE', 'CTLTYPE_INT'),
    'l1dcachesize': ('_HW_L1DCACHESIZE', 'CTLTYPE_QUAD'),
    'l1dcachesize_compat': ('HW_L1DCACHESIZE', 'CTLTYPE_INT'),
    'l2settings': ('HW_L2SETTINGS', 'CTLTYPE_INT'),
    'l2cachesize_compat': ('HW_L2CACHESIZE', 'CTLTYPE_INT'),
    'l3settings': ('HW_L3SETTINGS', 'CTLTYPE_INT'),
    'l3cachesize_compat': ('HW_L3CACHESIZE', 'CTLTYPE_INT'),
    'tbfrequency': ('_HW_TBFREQUENCY', 'CTLTYPE_QUAD'),
    'tbfrequency_compat': ('HW_TB_FREQ', 'CTLTYPE_INT'),
}

# hw.optional name -> (cpuid leaf, register, bit)
OPTIONAL = {
    'enfstrg': (7, 1, 9), 'rtm': (7, 1, 11), 'hle': (7, 1, 4), 'adx': (7, 1, 19),
    'mpx': (7, 1, 14), 'sgx': (7, 1, 2), 'avx512cd': (7, 1, 28),
    'avx512dq': (7, 1, 17), 'avx512bw': (7, 1, 30), 'avx512vl': (7, 1, 31),
    'avx512ifma': (7, 1, 21), 'avx512vbmi': (7, 2, 1),
}


def added_lines(path):
    patch = (ROOT / 'patches/xnu.patch').read_text()
    section = patch.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def function(source, signature):
    source += '\n'
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


def patched_lines(path):
    """Added and context lines: the table heads are upstream context."""
    patch = (ROOT / 'patches/xnu.patch').read_text()
    section = patch.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith(('+', ' ')) and not line.startswith('+++'))


# sys/sysctl.h numbers of the fixed hw OIDs the table uses
HW_OIDS = {'HW_MACHINE': 1, 'HW_MODEL': 2, 'HW_NCPU': 3, 'HW_BYTEORDER': 4, 'HW_PHYSMEM': 5,
           'HW_USERMEM': 6, 'HW_PAGESIZE': 7, 'HW_EPOCH': 10, 'HW_VECTORUNIT': 13,
           'HW_BUS_FREQ': 14, 'HW_CACHELINE': 16, 'HW_L1ICACHESIZE': 17,
           'HW_L1DCACHESIZE': 18, 'HW_L2SETTINGS': 19, 'HW_L2CACHESIZE': 20,
           'HW_L3SETTINGS': 21, 'HW_L3CACHESIZE': 22, 'HW_TB_FREQ': 23, 'HW_MEMSIZE': 24,
           'HW_AVAILCPU': 25}


def private_oids():
    """Values of the file's own _HW_* enumerators that the patch lets us compute.

    The patch holds hunks, not the whole file: an enumerator without an explicit
    value is numbered only when the one before it is in the same hunk. The
    others are left out, since the lines between hunks are not in this
    repository; the host check (tools/riot-host-sysctl-cpu-check.c) walks the
    complete compiled table for duplicates.
    """
    patch = (ROOT / 'patches/xnu.patch').read_text()
    section = patch.split(f'diff --git a/{SYSCTL_HW} b/{SYSCTL_HW}\n', 1)[1].split('\ndiff --git ', 1)[0]
    values = {}
    previous = None
    for line in section.splitlines():
        if line.startswith('@@'):
            previous = None  # a gap: the next implicit value is unknown
            continue
        if line.startswith('-') or line.startswith(('+++', '---')):
            continue
        match = re.match(r'[+ ]\t(_HW_\w+)(?: = (\d+))?,', line)
        if not match:
            if line[1:].startswith('};'):
                previous = None
            continue
        name, value = match.groups()
        if value is not None:
            previous = int(value)
        elif previous is not None:
            previous += 1
        else:
            continue
        values[name] = previous
    return values


def added_private_oids():
    """The _HW_* enumerators this patch adds."""
    return set(re.findall(r'^\t(_HW_\w+)(?: = \d+)?,', added_lines(SYSCTL_HW), flags=re.M))


def entries(optional):
    """(oid, name) of every hw.optional entry (numeric OIDs) or hw entry (named OIDs)."""
    found = re.findall(r'\{ \.oid = (\w+), \.type = \w+, \.exttype = "\w*", \.name = "(\w+)", \.(?:handler|subctls)',
                       patched_lines(SYSCTL_HW))
    return [(oid, name) for oid, name in found if oid.isdigit() == optional]


class HwSysctlLeavesTests(unittest.TestCase):
    def test_leaves_are_registered_with_xnu_oids_and_types(self):
        hw = added_lines(SYSCTL_HW)
        for name, (oid, kind) in LEAVES.items():
            self.assertRegex(
                hw,
                rf'\{{ \.oid = {oid}, \.type = {kind}, [^\n]*\.name = "{name}", ',
                name)

    def test_table_oids_and_names_are_unique(self):
        numbers = {**HW_OIDS, **private_oids()}
        for optional in (False, True):
            found = entries(optional)
            self.assertGreater(len(found), 25)
            names = [leaf for _, leaf in found]
            self.assertEqual(len(names), len(set(names)))
            spellings = [oid for oid, _ in found]
            self.assertEqual(len(spellings), len(set(spellings)))
            # the numbers the compiler sees, where the patch shows them, so a
            # private enumerator cannot land on a fixed HW_* OID unnoticed
            oids = [int(oid) if optional else numbers[oid] for oid, _ in found
                    if optional or oid in numbers]
            self.assertEqual(len(oids), len(set(oids)), sorted(oids))

    def test_every_added_private_oid_has_a_known_unique_number(self):
        private = private_oids()
        added = added_private_oids()
        self.assertIn('_HW_CPUFAMILY', added)
        self.assertIn('_HW_TARGETTYPE', added)
        self.assertLessEqual(added, set(private), added - set(private))
        # numbering stops at a hunk boundary instead of guessing
        self.assertEqual(private['_HW_PHYSICAL_CPU'], 1000)
        self.assertEqual(private['_HW_LOGICAL_CPU'], 1002)
        self.assertNotIn('_HW_CPUTHREADTYPE', private)
        values = [private[name] for name in added] + list(HW_OIDS.values())
        self.assertEqual(len(values), len(set(values)), sorted(values))

    def test_private_oids_stay_clear_of_the_fixed_range(self):
        private = private_oids()
        self.assertIn('_HW_CPUFAMILY', private)
        self.assertIn('_HW_TARGETTYPE', private)
        # HW_MAXID is 28; _HW_CPUFREQUENCY_COMPAT is XNU's HW_CPU_FREQ (15)
        for name, value in private.items():
            if name != '_HW_CPUFREQUENCY_COMPAT':
                self.assertGreaterEqual(value, 1000, name)
        self.assertEqual(private['_HW_CPUFREQUENCY_COMPAT'], 15)
        self.assertNotIn(15, HW_OIDS.values())

    def test_activecpu_has_xnu_numeric_oid(self):
        hw = patched_lines(SYSCTL_HW)
        self.assertIn('{ .oid = HW_AVAILCPU, .type = CTLTYPE_INT, .exttype = "", .name = "activecpu", ', hw)
        self.assertIn('{ .oid = _HW_AVAILCPU_NAME, .type = CTLTYPE_INT, .exttype = "", .name = "availcpu", ', hw)
        self.assertNotIn('_HW_ACTIVECPU', '\n'.join(
            line for line in hw.splitlines() if 'unused in this table before' not in line))

    def test_optional_features_check_the_host_maximum_leaf(self):
        body = function(patched_lines(SYSCTL_HW), 'static long optional_feature(')
        self.assertLess(body.index(': "0" (0), "2" (0));'), body.index(': "0" (leaf), "2" (0));'))
        self.assertIn('if (regs[0] < leaf)', body)

    def test_optional_features_use_the_right_cpuid_bits(self):
        hw = added_lines(SYSCTL_HW)
        for name, (leaf, reg, bit) in OPTIONAL.items():
            self.assertIn(f'optional_handler({name}, {leaf}, {reg}, {bit})', hw)
            self.assertIn(f'.name = "{name}", .handler = handle_optional_{name} }}', hw)

    def test_cpuid_trap_and_sysctls_share_the_leaf7_mask(self):
        header = added_lines(f'{EMULATION}/include/common/cpu_identity.h')
        trace = added_lines(f'{EMULATION}/src/linux_premigration/signal/cpuid_trace.c')
        self.assertIn('#define DARLING_COMPAT_LEAF7_EBX 0x029c6fbfu', header)
        self.assertIn('*ebx &= DARLING_COMPAT_LEAF7_EBX;', trace)
        self.assertNotIn('0x029c6fbf', trace)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_handlers_report_the_identity_part_and_xnu_sizes(self):
        header = added_lines(f'{EMULATION}/include/common/cpu_identity.h')
        table_source = added_lines(f'{EMULATION}/src/common/cpu_identity.c')
        start = table_source.index('#define DARLING_COMPAT_BRAND')
        identity = table_source[start:table_source.index('};', start) + 2]
        hw = added_lines(SYSCTL_HW)
        handlers = hw[hw.index('// SYSCTL_OUT: a short buffer is left untouched and gets ENOMEM with length 0.'):]
        compat_cache = function(hw, 'static bool compat_cache_level(')
        compat_optional = function(hw, 'static int compat_optional_feature(')

        harness = f"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <string.h>
{header}
{identity}
#define CPUFAMILY_INTEL_PENRYN      0x78ea4fbc
#define CPUFAMILY_INTEL_NEHALEM     0x6b5a4cd2
#define CPUFAMILY_INTEL_WESTMERE    0x573b5eec
#define CPUFAMILY_INTEL_SANDYBRIDGE 0x5490b78c
#define CPUFAMILY_INTEL_IVYBRIDGE   0x1f65e835
#define CPUFAMILY_INTEL_HASWELL     0x10b282dc
#define CPUFAMILY_INTEL_BROADWELL   0x582ed09c
#define CPUFAMILY_INTEL_SKYLAKE     0x37fc219f
#define CPUFAMILY_INTEL_KABYLAKE    0x0f817246
#define CPUFAMILY_INTEL_ICELAKE     0x38435547
#define sysctl_handler(_hname) long _hname(const int* name, int nlen, void* old, \\
    unsigned long* oldlen, void* _new, unsigned long newlen)

static int g_compat = 1;

/* the Mach pieces hw.usermem uses */
typedef int kern_return_t;
typedef unsigned int mach_port_t;
typedef unsigned int mach_msg_type_number_t;
#define KERN_SUCCESS 0
#define HOST_VM_INFO64 4
#define HOST_VM_INFO64_COUNT 38
struct vm_statistics64 {{ unsigned int free_count, active_count, inactive_count, wire_count; }};
static unsigned long long host_real_mem = 16ULL << 30;
static unsigned int g_wired_pages = 396695;
static kern_return_t g_statistics_result = KERN_SUCCESS;
static mach_port_t mach_host_self(void) {{ return 1; }}
static mach_port_t mach_task_self(void) {{ return 2; }}
static kern_return_t mach_port_deallocate(mach_port_t task, mach_port_t name) {{ return 0; }}
static kern_return_t _kernelrpc_host_statistics64(mach_port_t host, int flavor, void* info,
    mach_msg_type_number_t* count)
{{
    assert(flavor == HOST_VM_INFO64 && *count == HOST_VM_INFO64_COUNT);
    ((struct vm_statistics64*) info)->wire_count = g_wired_pages;
    return g_statistics_result;
}}
static unsigned long long g_memory = 16ULL << 30;
static unsigned long long g_host_l3 = 32ULL << 20;
struct host_basic_info {{ unsigned long long max_mem; }};
static const struct host_basic_info* gethostinfo(void)
{{
    static struct host_basic_info info;
    info.max_mem = g_memory;
    return &info;
}}
const struct darling_cpu_identity* darling_compat_cpu_identity(void) {{ return &g_compat_identity; }}
int darling_compat_profile_enabled(void) {{ return g_compat; }}
{compat_cache}
static bool host_cache(unsigned int level, bool instruction, unsigned long long* size, unsigned int* sharing)
{{
    *sharing = 2;
    if (level == 1) {{ *size = instruction ? 32768 : 49152; return true; }}
    if (level == 2) {{ *size = 1 << 20; return true; }}
    if (level == 3 && g_host_l3) {{ *size = g_host_l3; return true; }}
    return false;
}}
static bool cache_level(unsigned int level, unsigned long long* size, unsigned int* sharing)
{{
    if (darling_compat_profile_enabled())
        return compat_cache_level(level, size, sharing);
    return host_cache(level, false, size, sharing);
}}
{compat_optional}
{handlers}

#define CALL(handler) handler(0, 0, old, &len, 0, 0)

static long long quad(sysctl_handler((*handler)))
{{
    long long value = -1;
    unsigned long len = sizeof(value);
    void* old = &value;
    assert(CALL(handler) == 0 && len == sizeof(value));
    return value;
}}

static int integer(sysctl_handler((*handler)))
{{
    int value = -1;
    unsigned long len = sizeof(value);
    void* old = &value;
    assert(CALL(handler) == 0 && len == sizeof(value));
    return value;
}}

int main(void)
{{
    unsigned long len;
    void* old;

    /* compat: the i7-9750H of a MacBookPro16,1 */
    assert(integer(handle_cpufamily) == 0x0f817246);
    assert(integer(handle_cpusubfamily) == 0);
    assert(integer(handle_byteorder) == 1234);
    assert(integer(handle_vectorunit) == 1);
    assert((unsigned int) integer(handle_physmem) == 0x80000000u);
    assert(quad(handle_pagesize32) == 4096);
    assert(integer(handle_epoch) == 0);
    assert(quad(handle_busfrequency) == 100000000);
    assert(integer(handle_busfrequency_compat) == 100000000);
    assert(quad(handle_cachelinesize) == 64 && integer(handle_cachelinesize_compat) == 64);
    assert(quad(handle_l1icachesize) == 32768 && integer(handle_l1icachesize_compat) == 32768);
    assert(quad(handle_l1dcachesize) == 32768 && integer(handle_l1dcachesize_compat) == 32768);
    assert(integer(handle_l2settings) == 1 && integer(handle_l2cachesize_compat) == 262144);
    assert(integer(handle_l3settings) == 1 && integer(handle_l3cachesize_compat) == 12582912);
    assert(quad(handle_tbfrequency) == 1000000000 && integer(handle_tbfrequency_compat) == 1000000000);

    /* hw.usermem: a 16 GiB MacBookPro11,3 with 1.5 GiB wired reports 522620928 */
    assert(integer(handle_usermem) == 522620928);
    assert(usermem_value(16ULL << 30, 0) == (int) 0x80000000u);
    assert(usermem_value(16ULL << 30, 786432) == -1073741824); /* 3 GiB wired wraps, as in XNU */
    assert(usermem_value(1ULL << 30, 1024) == (1 << 30) - (4 << 20));
    /* the wired count is scaled like host_statistics64 when the profile is smaller than the host */
    assert(scaled_wired_pages(1000, 16ULL << 30, 32ULL << 30) == 500);
    assert(scaled_wired_pages(1000, 16ULL << 30, 16ULL << 30) == 1000);
    assert(scaled_wired_pages(1000, 32ULL << 30, 16ULL << 30) == 1000);
    assert(scaled_wired_pages(1000, 0, 16ULL << 30) == 1000);
    host_real_mem = 32ULL << 30;
    assert(integer(handle_usermem) == (int) (0x80000000u - (396695u / 2) * 4096u));
    host_real_mem = 16ULL << 30;
    g_statistics_result = 5;
    {{
        int value = -1;
        len = sizeof(value); old = &value;
        assert(CALL(handle_usermem) == -EINVAL);
    }}
    g_statistics_result = KERN_SUCCESS;
    /* a size query answers without the statistics RPC */
    g_statistics_result = 5;
    len = 0; old = 0;
    assert(CALL(handle_usermem) == 0 && len == 4);
    g_statistics_result = KERN_SUCCESS;
    /* an unknown memory size is an error, not a negative value */
    g_memory = 0;
    {{
        int value = -1;
        len = sizeof(value); old = &value;
        assert(CALL(handle_usermem) == -EINVAL && value == -1);
    }}
    g_memory = 16ULL << 30;

    /* a profile memory below 2 GiB is reported as it is */
    g_memory = 1ULL << 30;
    assert(integer(handle_physmem) == 1 << 30);
    g_memory = 16ULL << 30;

    /* size query, then the exact size */
    len = 0; old = 0;
    assert(CALL(handle_busfrequency) == 0 && len == 8);
    len = 0;
    assert(CALL(handle_cpufamily) == 0 && len == 4);
    len = 0;
    assert(CALL(handle_tbfrequency) == 0 && len == 8);

    /* sysctl_io_number leaves accept an int buffer */
    int small = -1;
    len = sizeof(small); old = &small;
    assert(CALL(handle_tbfrequency) == 0 && len == 4 && small == 1000000000);
    small = -1; len = sizeof(small);
    assert(CALL(handle_pagesize32) == 0 && len == 4 && small == 4096);

    /* SYSCTL_OUT leaves: a short buffer is untouched, ENOMEM, length 0
     * (sysctl_old_user in bsd/kern/kern_newsysctl.c) */
    small = -1; len = sizeof(small);
    assert(CALL(handle_busfrequency) == -ENOMEM && len == 0 && small == -1);
    char bytes[3] = {{ 0x55, 0x55, 0x55 }};
    len = 2; old = bytes;
    assert(CALL(handle_byteorder) == -ENOMEM && len == 0);
    assert(bytes[0] == 0x55 && bytes[1] == 0x55 && bytes[2] == 0x55);

    /* a larger buffer is trimmed to the value's size */
    long long wide[2] = {{ -1, -1 }};
    len = sizeof(wide); old = wide;
    assert(CALL(handle_cpufamily) == 0 && len == 4 && (int) wide[0] == 0x0f817246 && wide[1] == -1);

    /* cpuid_set_cpufamily(): family 6 models only */
    assert(cpufamily_from_signature(0x000906eau) == 0x0f817246); /* Coffee Lake-H */
    assert(cpufamily_from_signature(0x000806eau) == 0x0f817246); /* Kaby Lake-R */
    assert(cpufamily_from_signature(0x000506e3u) == 0x37fc219f); /* Skylake */
    assert(cpufamily_from_signature(0x000306c3u) == 0x10b282dc); /* Haswell */
    assert(cpufamily_from_signature(0x000706e5u) == 0x38435547); /* Ice Lake */
    assert(cpufamily_from_signature(0x00a20f12u) == 0);          /* Zen 3: family 0x19 */
    assert(cpufamily_from_signature(0x000b0671u) == 0);          /* Raptor Lake: not in XNU */

    /* compat leaf 7: the host's bits inside the Coffee Lake mask, nothing else */
    assert(compat_optional_feature(7, 1, 19, 0xffffffffu) == 1); /* ADX */
    assert(compat_optional_feature(7, 1, 19, 0u) == 0);
    assert(compat_optional_feature(7, 1, 9, 0xffffffffu) == 1);  /* ERMS */
    assert(compat_optional_feature(7, 1, 16, 0xffffffffu) == 0); /* AVX512F */
    assert(compat_optional_feature(7, 1, 17, 0xffffffffu) == 0); /* AVX512DQ */
    assert(compat_optional_feature(7, 1, 21, 0xffffffffu) == 0); /* AVX512IFMA */
    assert(compat_optional_feature(7, 1, 28, 0xffffffffu) == 0); /* AVX512CD */
    assert(compat_optional_feature(7, 1, 30, 0xffffffffu) == 0); /* AVX512BW */
    assert(compat_optional_feature(7, 1, 31, 0xffffffffu) == 0); /* AVX512VL */
    assert(compat_optional_feature(7, 2, 1, 0xffffffffu) == 0);  /* AVX512VBMI */

    char target[8] = "";
    len = sizeof(target); old = target;
    assert(CALL(handle_targettype) == 0 && len == 4 && strcmp(target, "Mac") == 0);
    /* size query, then a short buffer: untouched, ENOMEM, length 0 */
    len = 0; old = 0;
    assert(CALL(handle_targettype) == 0 && len == 4);
    memset(target, 0x55, sizeof(target));
    len = 2; old = target;
    assert(CALL(handle_targettype) == -ENOMEM && len == 0 && target[0] == 0x55 && target[1] == 0x55);

    /* host profile: the host's caches, the same 100 MHz base clock */
    g_compat = 0;
    assert(quad(handle_busfrequency) == 100000000);
    assert(quad(handle_l1icachesize) == 32768 && quad(handle_l1dcachesize) == 49152);
    assert(integer(handle_l2cachesize_compat) == 1 << 20);
    assert(integer(handle_l3settings) == 1 && integer(handle_l3cachesize_compat) == 32 << 20);
    assert(quad(handle_cachelinesize) == 64);

    /* a CPU without an L3: EINVAL for the settings and the size, as in XNU */
    g_host_l3 = 0;
    int value = -1;
    len = sizeof(value); old = &value;
    assert(CALL(handle_l3settings) == -EINVAL);
    assert(CALL(handle_l3cachesize_compat) == -EINVAL);
    assert(integer(handle_l2settings) == 1);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'hw.c'
            binary = Path(folder) / 'hw'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                            '-Wno-unused-function', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
