"""The in-process mach_vm_region_recurse path does not leak /proc/self/maps descriptors.

Compiles the patched function from patches/xnu.patch against fake Linux system calls; Darling is not started.
On kernels without PROCMAP_QUERY (before Linux 6.11) the ioctl fails with ENOTTY. The function used to forget
the descriptor while it was still open, so every query leaked one (2,604 during one match on a 6.8 kernel).
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'
TARGET = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/mach/impl/mach_traps.c'


def patched_lines(path):
    text = PATCH.read_text()
    start = text.index('+++ b/' + path + '\n')
    end = text.find('\ndiff --git ', start)
    section = text[start:end if end != -1 else len(text)]
    return '\n'.join(line[1:] for line in section.splitlines()[1:]
                     if line[:1] in ('+', ' ') and not line.startswith('+++'))


def local_region_source():
    source = patched_lines(TARGET)
    start = source.index('struct lregion_query {')
    end = source.index('return __darling_rpc_mach_vm_region_recurse(', start)
    end = source.index('\n}\n', end) + 3
    return source[start:end]


HARNESS = r'''
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
typedef int kern_return_t;
typedef unsigned int mach_port_name_t;
#define KERN_SUCCESS 0
#define KERN_INVALID_ADDRESS 1
#define KERN_NO_SPACE 3
#define VM_PROT_READ 1
#define VM_PROT_WRITE 2
#define VM_PROT_EXECUTE 4
#define VISIBLE
#define LINUX_ENOENT 2
#define LINUX_EBADF 9
#define LINUX_EINVAL 22
#define LINUX_ENOTTY 25
enum { __NR_access = 1000, __NR_open, __NR_read, __NR_close, __NR_fcntl, __NR_ioctl, __NR_getpid,
       __NR_clock_gettime, __NR_nanosleep };
static int open_fds[4096];
static long ioctl_result;
static int rpc_calls, opens;
static long fake_syscall(long nr, ...)
{
	va_list ap; va_start(ap, nr);
	long a = va_arg(ap, long), b = va_arg(ap, long), c = va_arg(ap, long);
	va_end(ap);
	(void) c;
	switch (nr) {
	case __NR_access: return strcmp((const char*) a, "/tmp/rclol-local-region-all") == 0 ? 0 : -2;
	case __NR_open:
		if (strcmp((const char*) a, "/proc/self/maps") != 0) return -2;
		for (int fd = 3; fd < 4096; ++fd) if (!open_fds[fd]) { open_fds[fd] = 1; ++opens; return fd; }
		return -24;
	case __NR_fcntl:
		for (int fd = (int) c; fd < 4096; ++fd) if (!open_fds[fd]) { open_fds[fd] = 1; return fd; }
		return -24;
	case __NR_close: if (a < 0 || a >= 4096 || !open_fds[a]) return -9; open_fds[a] = 0; return 0;
	case __NR_ioctl: return open_fds[a] ? ioctl_result : -9;
	case __NR_getpid: return 4242;
	case __NR_clock_gettime: memset((void*) b, 0, 16); return 0;
	default: return 0;
	}
}
#define LINUX_SYSCALL(...) fake_syscall(__VA_ARGS__)
static mach_port_name_t mach_task_self(void) { return 7; }
static int darling_is_league_game(void) { return 0; }
kern_return_t __darling_rpc_mach_vm_region_recurse(mach_port_name_t t, unsigned long long* a, unsigned long long* s,
	unsigned int* d, int* i, unsigned int* c) { (void) t; (void) a; (void) s; (void) d; (void) i; (void) c; ++rpc_calls; return 0; }
@SOURCE@
static int count_open(void) { int n = 0; for (int fd = 0; fd < 4096; ++fd) n += open_fds[fd]; return n; }
static void query(void)
{
	unsigned long long address = 0x1000, size = 0; unsigned int depth = 0, count = 19; int info[19];
	mach_vm_region_recurse(7, &address, &size, &depth, info, &count);
}
int main(int argc, char** argv)
{
	ioctl_result = argv[1][0] == 'n' ? -LINUX_ENOTTY : argv[1][0] == 'b' ? -LINUX_EBADF : 0;
	lregion_pace_us = 0;
	for (int i = 0; i < 500; ++i) query();
	printf("open=%d opens=%d rpc=%d\n", count_open(), opens, rpc_calls);
	return 0;
}
'''


@unittest.skipUnless(shutil.which('cc'), 'needs a C compiler')
class LocalRegionDescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        src = Path(cls.tmp.name) / 'region.c'
        src.write_text(HARNESS.replace('@SOURCE@', local_region_source()))
        cls.binary = Path(cls.tmp.name) / 'region'
        subprocess.run(['cc', '-std=gnu11', '-w', '-o', str(cls.binary), str(src)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, mode):
        out = subprocess.run([str(self.binary), mode], check=True, capture_output=True, text=True).stdout
        return dict(item.split('=') for item in out.split())

    def test_kernel_without_procmap_query_leaks_nothing_and_stops_trying(self):
        result = self.run_case('notty')
        self.assertEqual(result['open'], '0')
        self.assertEqual(result['opens'], '1')
        self.assertEqual(result['rpc'], '500')

    def test_descriptor_that_fails_after_opening_is_closed(self):
        result = self.run_case('badfd')
        self.assertEqual(result['open'], '0')
        self.assertEqual(result['rpc'], '500')

    def test_working_ioctl_keeps_one_descriptor_answered_locally(self):
        result = self.run_case('ok')
        self.assertEqual(result['open'], '1')
        self.assertEqual(result['opens'], '1')
        self.assertEqual(result['rpc'], '0')


if __name__ == '__main__':
    unittest.main()
