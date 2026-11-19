"""The guest's CPU number must stay below the CPU count the commpage reports.

libdispatch's continuation heap and libmalloc's nano allocator size per-CPU
arrays from _COMM_PAGE_LOGICAL_CPUS and index them with _os_cpu_number()
without a bounds check. Under profile=compat the commpage reports the
i7-9750H's 12 logical CPUs (8 with /tmp/rclol-fixed-cpu-count), but the CPU
number came straight from the host: on a 16-thread host every guest process
running on CPU 12-15 faulted at boot in _dispatch_alloc_continuation_alloc.
_os_cpu_number() and libmalloc's vDSO getcpu path now fold the host CPU into
the reported count.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CC = shutil.which('clang') or shutil.which('cc')


def new_side(patch, path):
    """Context and added lines of `path`'s section in `patch`."""
    sections = (ROOT / 'patches' / patch).read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in {patch}, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


TSD = new_side('xnu.patch', 'libsyscall/os/tsd.h')


def fold_function():
    """The patched _os_cpu_number_fold, from its signature to its closing brace."""
    start = TSD.index('static __inline__ unsigned int\n_os_cpu_number_fold(unsigned int cpu)')
    return TSD[start:TSD.index('\n}\n', start) + 3]


HARNESS = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
static uint8_t commpage_logical_cpus;
#define __DARLING_COMM_PAGE_LOGICAL_CPUS ((uintptr_t)&commpage_logical_cpus)
%(function)s
int main(int argc, char **argv) {
    commpage_logical_cpus = (uint8_t)atoi(argv[1]);
    for (unsigned cpu = 0; cpu < 64; cpu++)
        printf("%%u\n", _os_cpu_number_fold(cpu));
    return 0;
}
'''


class CpuNumberFoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if CC is None:
            raise unittest.SkipTest('no C compiler')
        cls.tmp = tempfile.TemporaryDirectory(prefix='cpu-number-fold-')
        source = Path(cls.tmp.name) / 'fold.c'
        cls.binary = Path(cls.tmp.name) / 'fold'
        source.write_text(HARNESS % {'function': fold_function()})
        subprocess.run([CC, '-O2', '-Wall', '-Werror', '-o', str(cls.binary), str(source)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def fold(self, count):
        out = subprocess.run([str(self.binary), str(count)], capture_output=True, text=True, check=True).stdout
        return [int(line) for line in out.split()]

    def test_compat_count_keeps_every_host_cpu_in_range(self):
        # 12 = the i7-9750H under profile=compat; 8 = the fixed-count opt-out.
        for count in (12, 8, 6, 1):
            with self.subTest(count=count):
                folded = self.fold(count)
                self.assertTrue(all(cpu < count for cpu in folded), folded)
                self.assertEqual(folded[:count], list(range(count)))  # in-range CPUs unchanged
                self.assertEqual(folded[count:count + 4], [c % count for c in range(count, count + 4)])

    def test_host_profile_count_is_the_identity(self):
        self.assertEqual(self.fold(64), list(range(64)))

    def test_unset_commpage_count_leaves_the_cpu_alone(self):
        self.assertEqual(self.fold(0), list(range(64)))


class CpuNumberCallersTests(unittest.TestCase):
    def test_os_cpu_number_folds_the_host_cpu(self):
        start = TSD.index('static __inline__ unsigned int\n_os_cpu_number(void)\n{\n#ifdef DARLING')
        body = TSD[start:TSD.index('#else // !DARLING', start)]
        self.assertIn('return _os_cpu_number_fold(_os_cpu_number_host());', body)

    def test_fold_reads_the_commpage_logical_cpu_count(self):
        # _COMM_PAGE_LOGICAL_CPUS in osfmk/i386/cpu_capabilities.h, which
        # libdispatch's dispatch_hw_config(logical_cpus) and libmalloc read.
        self.assertIn('#define __DARLING_COMM_PAGE_LOGICAL_CPUS 0x00007fffffe00036ULL', TSD)
        self.assertIn('__DARLING_COMM_PAGE_LOGICAL_CPUS', fold_function())

    def test_libmalloc_vdso_path_folds_the_host_cpu(self):
        malloc = new_side('libmalloc.patch', 'src/malloc.c')
        start = malloc.index('uint32_t malloc_cpu_number_native(void) {')
        body = malloc[start:malloc.index('\n}\n', start)]
        self.assertIn('return _os_cpu_number_fold(cpu);', body)
        self.assertNotRegex(body, r'return cpu;')


if __name__ == '__main__':
    unittest.main()
