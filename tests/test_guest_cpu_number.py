"""The guest CPU number stays inside the count the profile advertises.

profile=compat reports the i7-9750H's 12 logical CPUs while Linux may schedule
a guest thread on any of the host's cores. Darwin promises
0 <= _os_cpu_number() < hw.logicalcpu_max, and callers size per-CPU arrays
from that sysctl: libdispatch keeps one 2 MiB continuation magazine per CPU
and indexes that array with this value, so a host CPU number past 11 indexed
one magazine past the mapped heap and wrote into whatever mapping followed it.
_os_cpu_number() now folds the host number into the count mldr publishes in
the commpage, and libdispatch folds its magazine index by the same
hw.logicalcpu_max it sized the array with, so a stale commpage cannot
reintroduce the overrun.
"""
from pathlib import Path
import re
import shutil
import unittest

import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'libsyscall/os/tsd.h'
XNU = ROOT / 'build/darling-src/src/external/xnu'


def section():
    patch = (ROOT / 'patches/xnu.patch').read_text()
    marker = f'diff --git a/{TARGET} b/{TARGET}\n'
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added_lines():
    return [line[1:] for line in section().splitlines()
            if line.startswith('+') and not line.startswith('+++')]


def post_image():
    """The file as the first hunk leaves it: context and added lines."""
    lines = []
    for line in section().splitlines():
        if line.startswith(('index ', '--- ', '+++ ', '@@')):
            continue
        if line.startswith('-'):
            continue
        if line.startswith('+'):
            lines.append(line[1:])
        elif line.startswith(' '):
            lines.append(line[1:])
        elif not line:
            lines.append('')
    return '\n'.join(lines)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class GuestCpuNumberTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def fold_source(self):
        lines = added_lines()
        begin = lines.index('#ifdef DARLING')
        end = lines.index('#endif /* DARLING */')
        return '\n'.join(lines[begin:end + 1])

    def test_fold_keeps_cpu_numbers_inside_the_advertised_count(self):
        added = '\n'.join(added_lines())
        # Both the RDTSCP and the getcpu path must fold.
        self.assertIn('return __darling_fold_cpu_number(aux & 0xfff);', added)
        self.assertIn('return __darling_fold_cpu_number(cpu_num);', added)
        # The helper must sit at file scope: nesting it inside _os_cpu_number
        # makes clang reject the header ("function definition is not allowed
        # here") in every translation unit that includes os/tsd.h.
        image = post_image()
        helper = image.index('__darling_fold_cpu_number(unsigned int cpu)')
        self.assertLess(helper, image.index('_os_cpu_number(void)'))
        self.assertLess(image.index('#endif /* DARLING */'),
                        image.index('_os_cpu_number(void)'))
        self.run_c(r'''
#include <assert.h>
#include <stdint.h>

#define DARLING 1
static unsigned char advertised;
#define DARLING_COMM_PAGE_LOGICAL_CPUS \
	(*(const volatile uint8_t *)&advertised)
''' + self.fold_source() + r'''

int main(void)
{
	advertised = 12;                    /* profile=compat: 6 cores / 12 threads */
	assert(__darling_fold_cpu_number(0) == 0);
	assert(__darling_fold_cpu_number(11) == 11);
	assert(__darling_fold_cpu_number(12) == 0);   /* host thread 12 -> slot 0 */
	assert(__darling_fold_cpu_number(13) == 1);
	assert(__darling_fold_cpu_number(25) == 1);

	advertised = 8;                     /* /tmp/rclol-fixed-cpu-count */
	assert(__darling_fold_cpu_number(7) == 7);
	assert(__darling_fold_cpu_number(13) == 5);

	advertised = 0;                     /* no commpage: leave it alone */
	assert(__darling_fold_cpu_number(13) == 13);

	advertised = 12;                    /* a 12-thread host still line up */
	assert(__darling_fold_cpu_number(11) == 11);
	return 0;
}
''')

    def test_dispatch_cpu_number_shares_a_magazine_past_the_count(self):
        patch = (ROOT / 'patches/libdispatch.patch').read_text()
        section = patch.split(
            'diff --git a/src/shims/tsd.h b/src/shims/tsd.h\n', 1)[1]
        added = [line[1:] for line in section.splitlines()
                 if line.startswith('+') and not line.startswith('+++')]
        start = next(i for i, line in enumerate(added)
                     if 'unsigned int cpu = _os_cpu_number();' in line)
        end = next(i for i, line in enumerate(added) if line.strip() == 'return cpu;')
        self.run_c(r'''
#include <assert.h>

static unsigned int host_cpu;
static unsigned int hw_config_logical_cpus;
#define _os_cpu_number() host_cpu
#define dispatch_hw_config(name) hw_config_logical_cpus

static unsigned int
dispatch_cpu_number(void)
{
''' + '\n'.join(added[start:end + 1]) + r'''
}

int main(void)
{
	hw_config_logical_cpus = 12;
	host_cpu = 11; assert(dispatch_cpu_number() == 11);
	host_cpu = 12; assert(dispatch_cpu_number() == 0);
	host_cpu = 13; assert(dispatch_cpu_number() == 1);
	hw_config_logical_cpus = 0;             /* config not initialized yet */
	host_cpu = 13; assert(dispatch_cpu_number() == 13);
	return 0;
}
''')

    def test_fold_reads_the_commpage_byte_mldr_publishes(self):
        self.assertIn('0x00007fffffe00036ULL', '\n'.join(added_lines()))
        if not XNU.is_dir():
            self.skipTest('prepared Darling sources required')
        capabilities = (XNU / 'osfmk/i386/cpu_capabilities.h').read_text()
        base = re.search(r'#define\s+_COMM_PAGE64_BASE_ADDRESS\s+\(\s*(0x[0-9a-fA-F]+)',
                         capabilities)
        logical = re.search(
            r'#define\s+_COMM_PAGE_LOGICAL_CPUS\s+'
            r'\(_COMM_PAGE_START_ADDRESS\+(0x[0-9a-fA-F]+)\)', capabilities)
        self.assertIsNotNone(base, '64-bit commpage base definition missing')
        self.assertIsNotNone(logical, '_COMM_PAGE_LOGICAL_CPUS definition missing')
        address = int(base.group(1), 16) + int(logical.group(1), 16)
        self.assertIn(f'0x{address:016x}ULL', '\n'.join(added_lines()))


if __name__ == '__main__':
    unittest.main()
