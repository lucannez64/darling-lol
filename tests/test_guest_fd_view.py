"""The guest's view of its own descriptors matches a macOS application's.

Compiles the patched fd_view.c and getrlimit.c/setrlimit.c from patches/xnu.patch against fake Linux
system calls; never starts Darling. Measured on macOS 15.5: launchd gives applications 256 soft /
unlimited hard descriptors, and /dev/fd lists only the program's own descriptors.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'
EMU = 'darling/src/libsystem_kernel/emulation/'


def patched_file(path):
    text = PATCH.read_text()
    start = text.index('+++ b/' + path + '\n')
    end = text.find('\ndiff --git ', start)
    section = text[start:end if end != -1 else len(text)]
    return '\n'.join(line[1:] for line in section.splitlines()[1:]
                     if line[:1] in ('+', ' ') and not line.startswith('+++'))


def strip_includes(source):
    return '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))


FD_HARNESS = r'''
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
enum { __NR_getpid = 1, __NR_readlink };
typedef enum { guard_flag_prevent_close = 1 } guard_flags_t;
static int guarded_fd = 1048575;
static int guard_table_check(int fd, guard_flags_t f) { (void) f; return fd == guarded_fd; }
static const char* links[2000000];
static long fake(long nr, ...)
{
	va_list ap; va_start(ap, nr);
	const char* path = va_arg(ap, const char*); char* out = va_arg(ap, char*); long size = va_arg(ap, long);
	va_end(ap);
	if (nr == __NR_getpid) return 100;
	int pid, fd;
	/* /dev/fd is the inherited procfs, where this process is 5000; guest pids live under the prefix. */
	if (!strcmp(path, "/proc/self")) { strcpy(out, "5000"); return 4; }
	if (!strcmp(path, "/proc/self/fd/7")) { strcpy(out, "/proc/5000/fd"); return 13; }
	if (!strcmp(path, "/proc/self/fd/6")) { strcpy(out, "/proc/777/fd"); return 12; }
	if (!strcmp(path, "/proc/self/fd/9")) { strcpy(out, "/home/u/.darling/proc/42/fd"); return 27; }
	if (!strcmp(path, "/proc/self/fd/8")) { strcpy(out, "/home/u/dir"); return 11; }
	/* Other processes' descriptors are only read from the guest's own procfs. */
	if (sscanf(path, "/home/u/.darling/proc/%d/fd/%d", &pid, &fd) != 2 || fd >= 2000000 || !links[fd]) return -2;
	long n = strlen(links[fd]); if (n > size) n = size; memcpy(out, links[fd], n); return n;
}
#define LINUX_SYSCALL(...) fake(__VA_ARGS__)
#define __simple_sprintf sprintf
struct vchroot_unexpand_args { char path[4096]; };
static int vchroot_guest_proc(const char* path, char* out, unsigned size)
{
	if (strlen(path) + 17 > size) return -36;
	strcpy(out, "/home/u/.darling"); strcat(out, path); return 0;
}
static int vchroot_unexpand(struct vchroot_unexpand_args* a)
{
	const char* prefix = "/home/u/.darling";
	if (strncmp(a->path, prefix, strlen(prefix))) { memmove(a->path + 19, a->path, strlen(a->path) + 1); memcpy(a->path, "/Volumes/SystemRoot", 19); return 0; }
	memmove(a->path, a->path + strlen(prefix), strlen(a->path) - strlen(prefix) + 1);
	return 0;
}
@SOURCE@
int main(void)
{
	links[3] = "/Users/u/Library/Logs/game.log"; links[4] = "socket:[1]"; links[5] = "pipe:[2]";
	links[6] = "anon_inode:[eventpoll]"; links[9] = "/dev/nvidia0"; links[10] = "/dev/dri/renderD128";
	links[11] = "anon_inode:[eventfd]"; links[12] = "anon_inode:[perf_event]"; links[13] = "/proc/100/maps";
	links[14] = "/dev/shm/x"; links[15] = "/memfd:darling (deleted)";
	links[16] = "/home/u/.darling/proc/1/mounts"; links[17] = "/home/u/.darling/Users/u/save.dat";
	links[1048575] = "socket:[3]"; links[1048570] = "socket:[4]";
	int fds[] = {3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16, 17, 1048575, 1048570, 77};
	for (unsigned i = 0; i < sizeof(fds) / sizeof(*fds); ++i) printf("%d=%d ", fds[i], darling_fd_view_type(100, fds[i]));
	printf("other=%d ", darling_fd_view_type(200, 1048570));
	printf("dir7=%d dir8=%d dir6=%d dir9=%d\n", darling_fd_view_directory_pid(7), darling_fd_view_directory_pid(8),
	       darling_fd_view_directory_pid(6), darling_fd_view_directory_pid(9));
	return 0;
}
'''

RLIMIT_HARNESS = r'''
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include <errno.h>
enum { __NR_prlimit64 = 1, __NR_geteuid };
struct xnu_rlimit { unsigned long long rlim_cur, rlim_max; long _dummy[20]; };
#define XNU_RLIM_INFINITY 0x7FFFFFFFFFFFFFFFULL
#define LINUX_RLIM_INFINITY (~0ULL)
static int rlimit_is_infinity(unsigned long long v) { return v == XNU_RLIM_INFINITY; }
static unsigned long long rlimit_from_linux(unsigned long long v) { return v > XNU_RLIM_INFINITY ? XNU_RLIM_INFINITY : v; }
enum { LINUX_RLIMIT_CPU = 0, LINUX_RLIMIT_FSIZE, LINUX_RLIMIT_DATA, LINUX_RLIMIT_STACK, LINUX_RLIMIT_CORE,
       LINUX_RLIMIT_RSS, LINUX_RLIMIT_NPROC, LINUX_RLIMIT_NOFILE, LINUX_RLIMIT_MEMLOCK, LINUX_RLIMIT_AS,
       LINUX_RLIMIT_LOCKS, LINUX_RLIMIT_SIGPENDING, LINUX_RLIMIT_MSGQUEUE };
enum { BSD_RLIMIT_CPU = 0, BSD_RLIMIT_FSIZE, BSD_RLIMIT_DATA, BSD_RLIMIT_STACK, BSD_RLIMIT_CORE, BSD_RLIMIT_AS,
       BSD_RLIMIT_MEMLOCK, BSD_RLIMIT_NPROC, BSD_RLIMIT_NOFILE, BSD_RLIMIT_POSIX = 0x1000 };
static unsigned long long real[16][2];
static int euid = 1000;
static long fake(long nr, ...)
{
	va_list ap; va_start(ap, nr);
	long pid = va_arg(ap, long); long which = va_arg(ap, long);
	unsigned long long* in = va_arg(ap, unsigned long long*); unsigned long long* out = va_arg(ap, unsigned long long*);
	va_end(ap); (void) pid;
	if (nr == __NR_geteuid) return euid;
	if (out) { out[0] = real[which][0]; out[1] = real[which][1]; }
	if (in) { if (in[0] > in[1]) return -EINVAL; real[which][0] = in[0]; real[which][1] = in[1]; }
	return 0;
}
#define LINUX_SYSCALL(...) fake(__VA_ARGS__)
static long errno_linux_to_bsd(long e) { return e; }
static unsigned int rlimit_bsd_to_linux(unsigned int which)
{ which &= ~BSD_RLIMIT_POSIX; return which == BSD_RLIMIT_NOFILE ? LINUX_RLIMIT_NOFILE : (unsigned) -1; }
@SOURCE@
static void show(const char* label)
{
	struct xnu_rlimit r; sys_getrlimit(BSD_RLIMIT_NOFILE, &r);
	printf("%s=%llu/%s ", label, r.rlim_cur == XNU_RLIM_INFINITY ? 0ULL : r.rlim_cur,
	       r.rlim_max == XNU_RLIM_INFINITY ? "inf" : "fin");
}
int main(void)
{
	real[LINUX_RLIMIT_NOFILE][0] = real[LINUX_RLIMIT_NOFILE][1] = 1048576;
	real[LINUX_RLIMIT_MSGQUEUE][0] = real[LINUX_RLIMIT_MSGQUEUE][1] = 819200;
	show("default");
	struct xnu_rlimit r = { 10240, XNU_RLIM_INFINITY };
	printf("set=%ld ", sys_setrlimit(BSD_RLIMIT_NOFILE, &r));
	show("raised");
	printf("real=%llu ", real[LINUX_RLIMIT_NOFILE][0]);
	nofile_hard = XNU_RLIM_INFINITY; /* a new process image inherits only the carrier */
	show("inherited");
	r.rlim_cur = XNU_RLIM_INFINITY; sys_setrlimit(BSD_RLIMIT_NOFILE, &r); show("unlimited");
	r.rlim_cur = 300; r.rlim_max = 200; printf("einval=%d ", sys_setrlimit(BSD_RLIMIT_NOFILE, &r) == -EINVAL);
	r.rlim_cur = 100; r.rlim_max = 4096; sys_setrlimit(BSD_RLIMIT_NOFILE, &r); show("lowered");
	r.rlim_max = 8192; printf("eperm=%d\n", sys_setrlimit(BSD_RLIMIT_NOFILE, &r) == -EPERM);
	return 0;
}
'''


@unittest.skipUnless(shutil.which('cc'), 'needs a C compiler')
class GuestDescriptorViewTests(unittest.TestCase):
    def build(self, harness, source):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / 'case.c'
        src.write_text(harness.replace('@SOURCE@', source))
        binary = Path(tmp.name) / 'case'
        subprocess.run(['cc', '-std=gnu11', '-w', '-o', str(binary), str(src)], check=True)
        out = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
        return dict(item.split('=') for item in out.split())

    def test_only_the_programs_own_descriptors_are_visible(self):
        source = strip_includes(patched_file(EMU + 'src/xnu_syscall/bsd/impl/misc/fd_view.c'))
        result = self.build(FD_HARNESS, source)
        # vnode 1, socket 2, pshm 3, kqueue 5, pipe 6; -1 hidden.
        self.assertEqual(result['3'], '1')
        self.assertEqual(result['4'], '2')
        self.assertEqual(result['5'], '6')
        self.assertEqual(result['6'], '5')
        self.assertEqual(result['14'], '3')
        self.assertEqual(result['17'], '1')
        for hidden in ('9', '10', '11', '12', '13', '15', '16', '1048575', '1048570', '77', 'other'):
            self.assertEqual(result[hidden], '-1', hidden)
        self.assertEqual(result['dir7'], '100')
        self.assertEqual(result['dir8'], '0')
        self.assertEqual(result['dir6'], '0')
        self.assertEqual(result['dir9'], '42')

    def test_descriptor_limit_is_macos_and_never_touches_the_real_one(self):
        # The patch carries hunks, not whole files: compile the added NOFILE block and drive it the
        # way sys_getrlimit/sys_setrlimit now do for RLIMIT_NOFILE (checked below).
        getrlimit = patched_file(EMU + 'src/xnu_syscall/bsd/impl/misc/getrlimit.c')
        block = getrlimit[getrlimit.index('#define DARLING_NOFILE_DEFAULT_SOFT'):getrlimit.index('long sys_getrlimit(')]
        source = block + '''
static long sys_getrlimit(unsigned int which, struct xnu_rlimit* rlp) { (void) which; return darling_nofile_get(rlp); }
static long sys_setrlimit(unsigned int which, const struct xnu_rlimit* rlp) { (void) which; return darling_nofile_set(rlp); }
'''
        self.assertIn('if (which == LINUX_RLIMIT_NOFILE)\n\t\treturn darling_nofile_get(rlp);', getrlimit)
        self.assertIn('if (which == LINUX_RLIMIT_NOFILE)\n\t\treturn darling_nofile_set(rlp);',
                      patched_file(EMU + 'src/xnu_syscall/bsd/impl/misc/setrlimit.c'))
        result = self.build(RLIMIT_HARNESS, source)
        self.assertEqual(result['default'], '256/inf')
        self.assertEqual(result['set'], '0')
        self.assertEqual(result['raised'], '10240/inf')
        self.assertEqual(result['real'], '1048576')
        self.assertEqual(result['inherited'], '10240/inf')
        self.assertEqual(result['unlimited'], '0/inf')
        self.assertEqual(result['einval'], '1')
        self.assertEqual(result['lowered'], '100/fin')
        self.assertEqual(result['eperm'], '1')

    def test_dev_fd_listing_and_pidlistfds_use_the_same_view(self):
        dirent = patched_file(EMU + 'src/xnu_syscall/bsd/impl/dirent/getdirentries.c')
        self.assertEqual(dirent.count('if (!fd_entry_visible(fd_dir_pid, l64->d_name))'), 2)
        self.assertEqual(dirent.count('goto again;'), 2)
        info = patched_file(EMU + 'src/xnu_syscall/bsd/impl/misc/proc_info.c')
        self.assertIn('case PROC_PIDLISTFDS:', info)
        self.assertIn('type = darling_fd_view_type(pid, fd);', info)
        self.assertIn('(long) (highest + 1 + 20) * (long) sizeof(struct proc_fdinfo)', info)
        dtable = patched_file(EMU + 'src/xnu_syscall/bsd/impl/unistd/getdtablesize.c')
        self.assertIn('return min(lim.rlim_cur, DARLING_MAXFILESPERPROC);', dtable)

    def test_process_limit_is_bounded_by_kern_maxproc(self):
        getrlimit = patched_file(EMU + 'src/xnu_syscall/bsd/impl/misc/getrlimit.c')
        start = getrlimit.index('\tif (which == LINUX_RLIMIT_NPROC)\n')
        block = getrlimit[start:getrlimit.index('\n\t}\n', start) + 4]
        source = r'''
#include <stdio.h>
enum { LINUX_RLIMIT_NPROC = 6 };
struct xnu_rlimit { unsigned long long rlim_cur, rlim_max; };
static void darling_kern_proc_limits(int* maxproc, int* perid) { *maxproc = 4176; *perid = 2784; }
static void clamp(unsigned which, struct xnu_rlimit* rlp)
{
@BLOCK@
}
int main(void)
{
	struct xnu_rlimit host = { 126609, 126609 }, low = { 100, 200 }, other = { 126609, 126609 };
	clamp(LINUX_RLIMIT_NPROC, &host); clamp(LINUX_RLIMIT_NPROC, &low); clamp(7, &other);
	printf("host=%llu/%llu low=%llu/%llu other=%llu\n", host.rlim_cur, host.rlim_max,
	       low.rlim_cur, low.rlim_max, other.rlim_cur);
	return 0;
}
'''.replace('@BLOCK@', block)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'case.c'
            src.write_text(source)
            subprocess.run(['cc', '-std=gnu11', '-w', '-o', str(Path(tmp) / 'case'), str(src)], check=True)
            out = subprocess.run([str(Path(tmp) / 'case')], check=True, capture_output=True, text=True).stdout
        # macOS 15.5 on the reference Mac: ulimit -u is kern.maxprocperuid, ulimit -Hu kern.maxproc.
        self.assertEqual(out.split(), ['host=2784/4176', 'low=100/200', 'other=126609'])
        kern = patched_file(EMU + 'src/xnu_syscall/bsd/helper/misc/sysctl_kern.c')
        self.assertIn('void darling_kern_proc_limits(int* maxproc, int* maxprocperuid)', kern)



if __name__ == '__main__':
    unittest.main()
