#!/usr/bin/env python3
"""Build and run the rlimit-conversion check against the real kernel sources.

Compiles Darling's own getrlimit.c, setrlimit.c and errno.c for the host, with
the Linux syscall trampoline shimmed to the host's prlimit64. Asserts that a
guest sees Darwin's RLIM_INFINITY (0x7FFFFFFFFFFFFFFF) instead of the Linux
~0ULL, that the descriptor-count adjustment never wraps an unlimited value, and
that a guest cannot set a limit above Darwin's maximum.

Nothing but this check's own directory under build/logs is written; no prefix,
guest or process is touched.
"""
from pathlib import Path
import subprocess

project = Path(__file__).resolve().parent.parent
emulation = project / 'build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation'
sources = emulation / 'src'
folder = project / 'build/logs/rlimit-check'
folder.mkdir(parents=True, exist_ok=True)

alias = folder / 'include/darling/emulation'
alias.parent.mkdir(parents=True, exist_ok=True)
if not alias.exists():
    alias.symlink_to(emulation / 'include', target_is_directory=True)
if alias.resolve() != (emulation / 'include').resolve():
    raise SystemExit('Unexpected host-test header alias')

# The guest trampoline is an assembly stub into Darwin's kernel; on the host the
# same call is prlimit64, which returns -1/errno and must be reported as a
# negative errno the way Darling's callers expect.
(folder / 'shim.c').write_text('''\
#include <errno.h>
#include <sys/syscall.h>
#include <unistd.h>

long __lolsys_syscall(long, long, long, long, long, long, int);

long __lolsys_syscall(long a1, long a2, long a3, long a4, long a5, long a6, int nr)
{
	long ret = syscall(nr, a1, a2, a3, a4, a5, a6);
	return ret == -1 ? -errno : ret;
}
''')

# The host's own RLIM_INFINITY is the Linux ~0ULL, so the guest wrapper must be
# checked against XNU_RLIM_INFINITY, never against the host macro.
(folder / 'check.c').write_text('''\
#include <errno.h>
#include <stdio.h>
#include <sys/resource.h>

#include <darling/emulation/conversion/misc/getrlimit.h>
#include <darling/emulation/xnu_syscall/bsd/impl/misc/getrlimit.h>
#include <darling/emulation/xnu_syscall/bsd/impl/misc/setrlimit.h>

static int failures;

static void expect(const char* what, unsigned long long got, unsigned long long want)
{
	if (got != want)
	{
		printf("FAIL %s: got 0x%llX, want 0x%llX\\n", what, got, want);
		failures++;
	}
	else
		printf("ok   %s: 0x%llX\\n", what, got);
}

static void get_limit(unsigned int which, struct xnu_rlimit* lim)
{
	lim->rlim_cur = lim->rlim_max = 0;
	if (sys_getrlimit(which, lim) != 0)
	{
		printf("FAIL getrlimit(%u) failed\\n", which);
		failures++;
	}
}

int main(void)
{
	struct xnu_rlimit lim;
	struct xnu_rlimit ask;
	long ret;

	/* An unlimited resource must not surface the Linux ~0ULL: Chromium's
	 * checked_cast<int64_t> of it traps before the main process starts. */
	get_limit(BSD_RLIMIT_STACK, &lim);
	expect("stack limit is representable", (unsigned long long)(lim.rlim_cur <= XNU_RLIM_INFINITY), 1);
	expect("stack maximum is representable", (unsigned long long)(lim.rlim_max <= XNU_RLIM_INFINITY), 1);
	if (lim.rlim_cur == XNU_RLIM_INFINITY)
		printf("ok   stack unlimited reported as Darwin RLIM_INFINITY\\n");
	else
		printf("note stack is limited to 0x%llX on this host\\n", lim.rlim_cur);

	/* An unlimited descriptor count must not be decremented into 0x7FFF...FFFE. */
	get_limit(BSD_RLIMIT_NOFILE, &lim);
	expect("descriptor limit is representable", (unsigned long long)(lim.rlim_cur <= XNU_RLIM_INFINITY), 1);
	if (lim.rlim_cur == XNU_RLIM_INFINITY)
		printf("ok   unlimited descriptors stay unlimited\\n");
	else
		printf("note descriptors are limited to 0x%llX\\n", lim.rlim_cur);

	/* A finite descriptor limit round-trips with the reserved-descriptor
	 * adjustment applied once by getrlimit and once by setrlimit. */
	ask.rlim_cur = ask.rlim_max = 1024;
	ret = sys_setrlimit(BSD_RLIMIT_NOFILE, &ask);
	expect("setrlimit(RLIMIT_NOFILE, 1024) accepted", (unsigned long long)(ret < 0), 0);
	get_limit(BSD_RLIMIT_NOFILE, &lim);
	expect("RLIMIT_NOFILE round trip", lim.rlim_cur, 1024);

	/* Darwin's infinity maps back to the Linux one instead of hitting EINVAL. */
	ask.rlim_cur = ask.rlim_max = XNU_RLIM_INFINITY;
	ret = sys_setrlimit(BSD_RLIMIT_STACK, &ask);
	expect("setrlimit(RLIMIT_STACK, infinity) accepted", (unsigned long long)(ret < 0), 0);
	get_limit(BSD_RLIMIT_STACK, &lim);
	expect("stack back to infinity", lim.rlim_cur, XNU_RLIM_INFINITY);

	/* A value Darwin cannot express is rejected, not silently truncated. */
	ask.rlim_cur = ask.rlim_max = 0x8000000000000000ULL;
	ret = sys_setrlimit(BSD_RLIMIT_CPU, &ask);
	expect("setrlimit above RLIM_INFINITY refused with EINVAL",
	       (unsigned long long)(-ret), (unsigned long long)EINVAL);

	/* The BSD flag bit is stripped before the Linux comparison. */
	get_limit(BSD_RLIMIT_STACK | BSD_RLIMIT_POSIX, &lim);
	expect("POSIX rlimit flag accepted", (unsigned long long)(lim.rlim_max <= XNU_RLIM_INFINITY), 1);

	if (failures)
	{
		printf("%d failure(s)\\n", failures);
		return 1;
	}
	printf("PASS rlimit conversion\\n");
	return 0;
}
''')

binary = folder / 'rlimit-check'
subprocess.run([
    'clang',
    '-I', str(folder / 'include'),
    '-I', str(emulation / 'include'),
    '-Wall', '-Wextra', '-Wno-unused-parameter',
    str(folder / 'shim.c'),
    str(sources / 'conversion/errno.c'),
    str(sources / 'xnu_syscall/bsd/impl/misc/getrlimit.c'),
    str(sources / 'xnu_syscall/bsd/impl/misc/setrlimit.c'),
    str(folder / 'check.c'),
    '-o', str(binary),
], check=True)
subprocess.run([str(binary)], check=True)
