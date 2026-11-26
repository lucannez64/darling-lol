"""The emulated sysctl tree has the keys and values macOS 15.5 (24F74) reports on
an Intel MacBookPro16,1; source-only, reads patches/xnu.patch, never starts Darling."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'
MISC = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/helper/misc/'


def added_file(path):
    """Return the added/kept lines of every hunk for path in patches/xnu.patch."""
    text = PATCH.read_text()
    marker = '+++ b/' + path + '\n'
    sections = []
    start = text.find(marker)
    while start != -1:
        end = text.find('\ndiff --git ', start)
        section = text[start:end if end != -1 else len(text)]
        sections.append('\n'.join(line[1:] for line in section.splitlines()[1:]
                                  if line[:1] in ('+', ' ') and not line.startswith('+++')))
        start = text.find(marker, start + 1)
    if not sections:
        raise AssertionError(path + ' has no section in patches/xnu.patch')
    return '\n'.join(sections)


def table_entry(source, name):
    match = re.search(r'\{ \.oid = (\w+), \.type = (\w+), \.exttype = "([^"]*)", '
                      r'\.name = "' + re.escape(name) + r'", \.handler = (\w+) \}', source)
    if match is None:
        raise AssertionError('no table entry for ' + name)
    return match.groups()


class XnuScaledLimits:
    """XNU's scale_setup() / bsd_scale_setup() for a non-server x86_64 Mac."""

    def __init__(self, memory):
        gib = 1024 ** 3
        scale = 0
        if memory >= 3 * gib:
            scale = 2 if memory < 8 * gib else min(16, memory // (4 * gib))
        self.maxproc, self.maxprocperuid = 20 + 32 * 32, (20 + 32 * 32) // 2
        self.maxfiles, self.maxfilesperproc = 10240 + 2048, 10240
        self.num_tasks, self.num_threads, self.num_taskthreads = 1024, 2560, 2560
        if scale:
            self.maxproc *= scale
            self.maxprocperuid = self.maxproc * 2 // 3
            if scale > 2:
                self.maxfiles *= scale
                self.maxfilesperproc = self.maxfiles // 2
            self.num_tasks = self.num_taskthreads = 1024 * scale
            self.num_threads = self.num_tasks * 5


class KernSysctlTests(unittest.TestCase):
    def setUp(self):
        self.source = added_file(MISC + 'sysctl_kern.c')

    def test_measured_keys_are_registered(self):
        expected = {
            'osproductversioncompat': ('CTLTYPE_STRING', 'handle_osproductversioncompat'),
            'iossupportversion': ('CTLTYPE_STRING', 'handle_iossupportversion'),
            'bootargs': ('CTLTYPE_STRING', 'handle_bootargs'),
            'hv_support': ('CTLTYPE_INT', 'handle_hv_support'),
            'secure_kernel': ('CTLTYPE_INT', 'handle_secure_kernel'),
            'securelevel': ('CTLTYPE_INT', 'handle_securelevel'),
            'ngroups': ('CTLTYPE_INT', 'handle_ngroups'),
            'development': ('CTLTYPE_INT', 'handle_development'),
            # maxproc keeps its upstream table line (only its handler changed), so it is not in the patch.
            'maxprocperuid': ('CTLTYPE_INT', 'handle_maxprocperuid'),
            'maxfiles': ('CTLTYPE_INT', 'handle_maxfiles'),
            'maxfilesperproc': ('CTLTYPE_INT', 'handle_maxfilesperproc'),
            'num_tasks': ('CTLTYPE_INT', 'handle_num_tasks'),
            'num_threads': ('CTLTYPE_INT', 'handle_num_threads'),
            'num_taskthreads': ('CTLTYPE_INT', 'handle_num_taskthreads'),
        }
        for name, (ctltype, handler) in expected.items():
            _, actual_type, _, actual_handler = table_entry(self.source, name)
            self.assertEqual((actual_type, actual_handler), (ctltype, handler), name)

    def test_xnu_mibs_where_macos_has_fixed_oids(self):
        for name, oid in (('maxfiles', 'KERN_MAXFILES'), ('maxfilesperproc', 'KERN_MAXFILESPERPROC'),
                          ('maxprocperuid', 'KERN_MAXPROCPERUID'), ('ngroups', 'KERN_NGROUPS'),
                          ('securelevel', 'KERN_SECURELVL')):
            self.assertEqual(table_entry(self.source, name)[0], oid, name)

    def test_measured_values(self):
        self.assertIn('copyout_string("10.16", (char*) old, oldlen);', self.source)
        self.assertIn('copyout_string("18.5", (char*) old, oldlen);', self.source)
        self.assertIn('copyout_string("", (char*) old, oldlen);', self.source)
        self.assertIn('handle_ngroups) { return copyout_int(16, old, oldlen); }', self.source)
        self.assertIn('handle_hv_support) { return copyout_int(1, old, oldlen); }', self.source)
        for name in ('securelevel', 'secure_kernel', 'development'):
            self.assertIn('handle_%s) { return copyout_int(0, old, oldlen); }' % name, self.source)

    def test_osvariant_status_matches_macos_15_5(self):
        self.assertIn('0x70010000f388828aULL', self.source)
        self.assertNotIn('0x70000000f288828aULL', self.source)

    def test_limits_follow_xnu_memory_scaling_not_host_pid_max(self):
        self.assertNotIn('/proc/sys/kernel/pid_max', self.source)
        self.assertIn('darling_hw_memsize(&memory, &host_real);', self.source)
        for constant in ('#define XNU_X86_64_NPROC (20 + 32 * 32)', '#define XNU_OPEN_MAX 10240',
                         '#define XNU_CONFIG_TASK_MAX 1024', '#define XNU_CONFIG_THREAD_MAX 2560'):
            self.assertIn(constant, self.source)
        limits = XnuScaledLimits(16 * 1024 ** 3)
        # The values the comment documents for the emulated 16 GB MacBookPro16,1.
        self.assertEqual((limits.maxproc, limits.maxprocperuid, limits.maxfiles, limits.maxfilesperproc,
                          limits.num_tasks, limits.num_threads, limits.num_taskthreads),
                         (4176, 2784, 49152, 24576, 4096, 20480, 4096))
        self.assertIn('maxproc 4176, maxprocperuid 2784,\n * maxfiles 49152, maxfilesperproc 24576, '
                      'num_tasks 4096, num_threads 20480,\n * num_taskthreads 4096.', self.source)


class HwSysctlTests(unittest.TestCase):
    def setUp(self):
        self.source = added_file(MISC + 'sysctl_hw.c')

    def test_intel_keys(self):
        self.assertEqual(table_entry(self.source, 'byteorder')[0], 'HW_BYTEORDER')
        self.assertEqual(table_entry(self.source, 'cachelinesize')[1:3], ('CTLTYPE_QUAD', 'Q'))
        self.assertEqual(table_entry(self.source, 'cachelinesize_compat')[0], 'HW_CACHELINE')
        self.assertEqual(table_entry(self.source, 'cpufamily')[1], 'CTLTYPE_INT')
        self.assertIn('*((int*) old) = 1234;', self.source)
        self.assertIn('#define INTEL_CACHE_LINE_SIZE 64', self.source)
        self.assertIn('*((unsigned int*) old) = CPUFAMILY_INTEL_KABYLAKE;', self.source)

    def test_no_arm_only_targettype(self):
        self.assertNotIn('"targettype"', self.source)


class VmSysctlTests(unittest.TestCase):
    def setUp(self):
        self.source = added_file(MISC + 'sysctl_vm.c')

    def test_loadavg_and_swapusage(self):
        self.assertEqual(table_entry(self.source, 'loadavg')[0:3], ('VM_LOADAVG', 'CTLTYPE_STRUCT', 'S,loadavg'))
        self.assertEqual(table_entry(self.source, 'swapusage')[0:3],
                         ('VM_SWAPUSAGE', 'CTLTYPE_STRUCT', 'S,xsw_usage'))
        self.assertIn('#define XNU_FSCALE 2048', self.source)
        self.assertIn('"/proc/loadavg"', self.source)
        self.assertIn('PARSE("SwapTotal:", swap_total)', self.source)
        self.assertIn('PARSE("SwapFree:", swap_free)', self.source)
        self.assertIn('usage->xsu_pagesize = 4096;', self.source)
        self.assertIn('usage->xsu_encrypted = 1;', self.source)


class SysctlNodeTests(unittest.TestCase):
    def test_proc_native(self):
        source = added_file(MISC + 'sysctl_sysctl.c')
        self.assertEqual(table_entry(source, 'proc_native')[3], 'handle_proc_native')

    def test_next_enumerates_the_tree(self):
        source = added_file(MISC + 'sysctl_unspec.c')
        self.assertEqual(table_entry(source, 'next')[0], '_NEXT')
        self.assertIn('_NEXT = 2,', source)
        self.assertIn('next_walk(&sysctls_root, path, 0, &search);', source)
        self.assertIn('return -ENOENT;', source)


if __name__ == '__main__':
    unittest.main()
