"""Run the patched launchd reaper against tracked and orphaned child fixtures."""
from pathlib import Path
import shutil
import unittest

import test_runtime_compatibility as sources


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class OrphanReapingTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def test_only_untracked_zombie_children_are_reaped(self):
        patch = (Path(__file__).resolve().parents[1] / 'patches/darling.patch').read_text()
        section = patch.split('diff --git a/src/launchd/src/core.c ', 1)[1]
        added = '\n'.join(line[1:] for line in section.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        reaper = sources.function(added.replace('void\njobmgr_reap_orphaned_children',
                                                'void jobmgr_reap_orphaned_children'),
                                 'jobmgr_reap_orphaned_children')
        self.run_c(r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef int pid_t;
typedef struct { int unused; } DIR;
struct dirent { char d_name[16]; };
struct proc_bsdshortinfo { int pbsi_ppid, pbsi_status; };
struct job { int pid; bool anonymous; };
struct manager {
    struct job *job;
    struct manager *child;
};
typedef struct manager *jobmgr_t;
typedef struct job *job_t;
#define SZOMB 5
#define PROC_PIDT_SHORTBSDINFO 1
#define PROC_PIDT_SHORTBSDINFO_SIZE sizeof(struct proc_bsdshortinfo)
#define WNOHANG 1
#define LOG_DEBUG 1
#define jobmgr_log(...) ((void)0)
static jobmgr_t root_jobmgr;
static unsigned cursor, waited;
static DIR directory;
static DIR *opendir(const char *path) {
    assert(!strcmp(path, "/proc")); cursor = 0; return &directory;
}
static struct dirent *readdir(DIR *dir) {
    static struct dirent entry;
    assert(dir == &directory);
    if (++cursor > 6) return NULL;
    snprintf(entry.d_name, sizeof(entry.d_name), "%u", cursor);
    return &entry;
}
static int closedir(DIR *dir) { assert(dir == &directory); return 0; }
static pid_t getpid(void) { return 100; }
static int proc_pidinfo(pid_t pid, int flavor, int arg, void *buffer, size_t size) {
    struct proc_bsdshortinfo *info = buffer;
    (void)flavor; (void)arg; assert(size == sizeof(*info));
    info->pbsi_ppid = pid == 5 ? 200 : 100;
    info->pbsi_status = pid == 6 ? 1 : SZOMB;
    return sizeof(*info);
}
static job_t jobmgr_find_by_pid(jobmgr_t manager, pid_t pid, bool create) {
    assert(!create);
    return manager->job && manager->job->pid == pid ? manager->job : NULL;
}
static job_t jobmgr_find_by_pid_deep(jobmgr_t manager, pid_t pid, bool anon_okay) {
    job_t job = jobmgr_find_by_pid(manager, pid, false);
    if (job && (!job->anonymous || anon_okay)) return job;
    return manager->child ? jobmgr_find_by_pid_deep(manager->child, pid, anon_okay) : NULL;
}
static pid_t waitpid(pid_t pid, int *status, int flags) {
    assert(flags == WNOHANG); *status = 0; waited |= 1u << pid; return pid;
}
''' + reaper + r'''
int main(void) {
    struct job root_job = {1, false}, session_job = {2, false}, anonymous_job = {3, true};
    struct manager nested = {&anonymous_job, NULL};
    struct manager session = {&session_job, &nested};
    struct manager root = {&root_job, &session};
    root_jobmgr = &root;
    jobmgr_reap_orphaned_children();
    /* Root, session and anonymous jobs retain their wait status. Only PID 4
     * is an untracked zombie child; PID 5 has another parent, PID 6 is alive. */
    assert(waited == (1u << 4));
    root_jobmgr = NULL;
    jobmgr_reap_orphaned_children();
    assert(waited == (1u << 4));
}
''')


if __name__ == '__main__':
    unittest.main()
