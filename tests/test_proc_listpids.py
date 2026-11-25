"""proc_listpids() answers like macOS instead of failing.

Darling's sys_proc_info() had no case for callnum 1 (proc_listpids): it printed
"sys_proc_info(): Unsupported callnum: 1" and returned ENOTSUP, which a Mac
never does. The League Client and its integrity framework list processes at
client start and around ready check (issue #31). proc_listpids() now reports
the same guest processes as sysctl KERN_PROC_ALL, and that list leaves out
anything without a guest Mach-O name (mldr before exec, host processes), so a
listed pid never resolves to a Linux-side name.
"""
from pathlib import Path
import shutil
import unittest

import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]
BSD = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd'
PROC_INFO = f'{BSD}/impl/misc/proc_info.c'
SYSCTL_PROC = f'{BSD}/helper/misc/sysctl_proc.c'


def section(target):
    patch = (ROOT / 'patches/xnu.patch').read_text()
    marker = f'diff --git a/{target} b/{target}\n'
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added(target):
    return '\n'.join(line[1:] for line in section(target).splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def listpids_source():
    text = added(PROC_INFO)
    start = text.index('static long _proc_listpids(uint32_t type, uint32_t typeinfo, void* buffer, int32_t bufsize)\n{')
    end = text.index('\n}\n', start) + 3
    return text[start:end]


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class ProcListPidsTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def test_callnum_1_is_dispatched_not_rejected(self):
        text = added(PROC_INFO)
        self.assertIn('case 1: // proc_listpids', text)
        self.assertIn('return _proc_listpids((uint32_t) pid, flavor, buffer, bufsize);', text)

    def test_nameless_processes_are_left_out_of_the_process_list(self):
        text = added(SYSCTL_PROC)
        self.assertIn('goto retnull;', text)
        removed = '\n'.join(line[1:] for line in section(SYSCTL_PROC).splitlines()
                            if line.startswith('-') and not line.startswith('---'))
        # The old fallback copied Linux's comm (e.g. "mldr") into p_comm.
        self.assertIn('strncpy(kinfo->kinfo.kp_proc.p_comm, elem', removed)

    def test_listpids_filters_and_sizes_like_xnu(self):
        self.run_c(r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#define PROC_ALL_PIDS 1
#define PROC_PGRP_ONLY 2
#define PROC_TTY_ONLY 3
#define PROC_UID_ONLY 4
#define PROC_RUID_ONLY 5
#define PROC_PPID_ONLY 6
#define PROC_KDBG_ONLY 7
#define KERN_PROC_ALL 0
#define P_CONTROLT 0x2

struct kinfo_proc {
	struct { pid_t p_pid; int p_flag; } kp_proc;
	struct {
		pid_t e_ppid; pid_t e_pgid; dev_t e_tdev;
		struct { uid_t cr_uid; } e_ucred;
		struct { uid_t p_ruid; } e_pcred;
	} kp_eproc;
};

static struct kinfo_proc table[4];
static int ntable;
static int grow_once;

int _sysctl_proc(int what, int flag, struct kinfo_proc* out, unsigned long* buflen)
{
	unsigned long need;
	(void) what; (void) flag;
	if (out != NULL && grow_once) {
		/* A process started between the size query and the fill. */
		grow_once = 0;
		ntable++;
	}
	need = ntable * sizeof(struct kinfo_proc);
	if (out == NULL || *buflen < need) {
		int full = out != NULL && *buflen < need;
		*buflen = need;
		return full ? -ENOSPC : 0;
	}
	memcpy(out, table, need);
	*buflen = need;
	return 0;
}

static void proc(int i, pid_t pid, pid_t ppid, pid_t pgid, uid_t uid, dev_t tty)
{
	memset(&table[i], 0, sizeof(table[i]));
	table[i].kp_proc.p_pid = pid;
	table[i].kp_eproc.e_ppid = ppid;
	table[i].kp_eproc.e_pgid = pgid;
	table[i].kp_eproc.e_ucred.cr_uid = uid;
	table[i].kp_eproc.e_pcred.p_ruid = uid;
	table[i].kp_eproc.e_tdev = tty;
	if (tty)
		table[i].kp_proc.p_flag |= P_CONTROLT;
}

''' + listpids_source() + r'''

int main(void)
{
	int pids[8];
	long n;

	/* /proc order: ascending pids. */
	proc(0, 1, 0, 1, 0, 0);          /* launchd */
	proc(1, 40, 1, 40, 501, 0);      /* RiotClientServices */
	proc(2, 41, 40, 40, 501, 0x400); /* LeagueClient, on a tty */
	ntable = 3;

	/* NULL buffer: the size needed plus room for 20 more, like xnu. */
	assert(_proc_listpids(PROC_ALL_PIDS, 0, NULL, 0) == (3 + 20) * (long) sizeof(int));

	/* Every pid, newest first. */
	n = _proc_listpids(PROC_ALL_PIDS, 0, pids, sizeof(pids));
	assert(n == 3 * (long) sizeof(int));
	assert(pids[0] == 41 && pids[1] == 40 && pids[2] == 1);

	/* A short buffer is filled, not overrun. */
	memset(pids, 0x7f, sizeof(pids));
	assert(_proc_listpids(PROC_ALL_PIDS, 0, pids, 2 * sizeof(int)) == 2 * (long) sizeof(int));
	assert(pids[2] == 0x7f7f7f7f);

	n = _proc_listpids(PROC_PPID_ONLY, 40, pids, sizeof(pids));
	assert(n == (long) sizeof(int) && pids[0] == 41);
	n = _proc_listpids(PROC_PGRP_ONLY, 40, pids, sizeof(pids));
	assert(n == 2 * (long) sizeof(int) && pids[0] == 41 && pids[1] == 40);
	n = _proc_listpids(PROC_UID_ONLY, 501, pids, sizeof(pids));
	assert(n == 2 * (long) sizeof(int));
	n = _proc_listpids(PROC_RUID_ONLY, 0, pids, sizeof(pids));
	assert(n == (long) sizeof(int) && pids[0] == 1);
	n = _proc_listpids(PROC_TTY_ONLY, 0x400, pids, sizeof(pids));
	assert(n == (long) sizeof(int) && pids[0] == 41);
	/* tty 0 means no controlling terminal, never a match. */
	assert(_proc_listpids(PROC_TTY_ONLY, 0, pids, sizeof(pids)) == 0);
	assert(_proc_listpids(PROC_KDBG_ONLY, 0, pids, sizeof(pids)) == 0);

	/* Unknown types are rejected, as in xnu. */
	assert(_proc_listpids(0, 0, pids, sizeof(pids)) == -EINVAL);
	assert(_proc_listpids(8, 0, pids, sizeof(pids)) == -EINVAL);

	/* A process that appears mid-call is still listed. */
	proc(3, 42, 41, 40, 501, 0);
	grow_once = 1;
	ntable = 3;
	n = _proc_listpids(PROC_ALL_PIDS, 0, pids, sizeof(pids));
	assert(n == 4 * (long) sizeof(int) && pids[0] == 42);
	return 0;
}
''')

    def test_darling_process_name_rejects_unknown_pids_even_with_cmdline(self):
        text = added(SYSCTL_PROC)
        start = text.index('static const char* path_basename(')
        end = text.index('\n}\n', text.index('bool darling_process_name(')) + 3
        fn = text[start:end]
        self.run_c(r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int rpc_rc = 0;
static const char* rpc_path = "";
static const char* fake_cmdline = "";
static int read_calls = 0;

static int dserver_rpc_get_executable_path(int pid, char* buf, size_t size, uint64_t* full)
{
	(void) pid;
	if (rpc_rc != 0)
		return rpc_rc;
	snprintf(buf, size, "%s", rpc_path);
	*full = strlen(rpc_path);
	return 0;
}

static int __simple_sprintf(char* buf, const char* fmt, int pid)
{
	return sprintf(buf, fmt, pid);
}

static bool read_string(const char* path, char* dst, int maxlen)
{
	(void) path;
	read_calls++;
	if (!fake_cmdline || !*fake_cmdline)
		return false;
	snprintf(dst, (size_t) maxlen, "%s", fake_cmdline);
	return true;
}

''' + fn + r'''

int main(void)
{
	char out[32];

	/* Unknown pid (ESRCH from darlingserver): must return false even if /proc/<pid>/cmdline has a name. */
	rpc_rc = ESRCH;
	fake_cmdline = "/usr/bin/bash";
	read_calls = 0;
	assert(!darling_process_name(99, out, sizeof(out)));
	assert(read_calls == 0);

	/* Known guest pid with Mach-O path from darlingserver. */
	rpc_rc = 0;
	rpc_path = "/Applications/LeagueClient.app/Contents/MacOS/LeagueClient";
	assert(darling_process_name(41, out, sizeof(out)));
	assert(strcmp(out, "LeagueClient") == 0);

	/* Known guest pid with empty path falls back to cmdline argv[0]. */
	rpc_rc = 0;
	rpc_path = "";
	fake_cmdline = "/sbin/launchd";
	assert(darling_process_name(1, out, sizeof(out)));
	assert(strcmp(out, "launchd") == 0);

	/* Un-exec'd mldr is rejected even when known. */
	rpc_rc = 0;
	rpc_path = "";
	fake_cmdline = "/usr/libexec/darling/mldr";
	assert(!darling_process_name(2, out, sizeof(out)));
	return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
