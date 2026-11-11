"""M7 SIP gates: every mutating wrapper must consult the protected-path check."""
from pathlib import Path
import re
import shutil
import unittest

import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]
BASE = 'darling/src/libsystem_kernel/emulation/'

# Wrapper file -> check it has to call before touching the file.
GATED = {
    BASE + 'src/xnu_syscall/bsd/impl/xattr/removexattr.c': 'dc_sip_check_write',
    BASE + 'src/xnu_syscall/bsd/impl/xattr/fremovexattr.c': 'dc_sip_check_fd',
    BASE + 'src/xnu_syscall/bsd/impl/unistd/chmod_extended.c': 'dc_sip_check_write',
    BASE + 'src/xnu_syscall/bsd/impl/unistd/fchmod_extended.c': 'dc_sip_check_fd',
    BASE + 'include/xnu_syscall/bsd/helper/xattr/setattrlist_generic.c': 'dc_sip_check_',
    BASE + 'src/xnu_syscall/bsd/impl/stat/mkfifo.c': 'dc_sip_check_write',
    BASE + 'src/xnu_syscall/bsd/impl/unistd/mknod.c': 'dc_sip_check_write',
}

STUBS = r'''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <string.h>
#include <time.h>

#define VCHROOT_FOLLOW 1
#define XATTR_NOFOLLOW 1
#define LINUX_AT_FDCWD (-100)
#define LINUX_AT_SYMLINK_NOFOLLOW 0x100
#define LINUX_UTIME_OMIT ((1l << 30) - 2l)
#define LINUX_S_IFIFO 0010000
#define KAUTH_UID_NONE (-2)
#define KAUTH_GID_NONE (-2)

#define __NR_chmod 1
#define __NR_fchmod 2
#define __NR_fchown 3
#define __NR_chown 4
#define __NR_fchownat 5
#define __NR_fchmodat 6
#define __NR_mknod 7
#define __NR_mknodat 8
#define __NR_lremovexattr 9
#define __NR_removexattr 10
#define __NR_fremovexattr 11
#define __NR_utimensat 12
#define __NR_fchmod_extended 13

struct vchroot_expand_args {
    char path[4096];
    int flags;
    int dfd;
};

struct xnu_attrlist {
    unsigned short bitmapcount;
    unsigned int commonattr;
    unsigned int volattr;
    unsigned int dirattr;
    unsigned int fileattr;
    unsigned int forkattr;
};

static int sip_calls;
static int sys_calls;
static int sip_result;
static int syscall_result;

static int dc_sip_check_write(const char* path) { (void) path; ++sip_calls; return sip_result; }
static int dc_sip_check_fd(int fd) { (void) fd; ++sip_calls; return sip_result; }
static int errno_linux_to_bsd(int value) { return value; }
static int xattr_errno_to_bsd(int value) { return value; }
static const char* xattr_name_to_linux(const char* name, char* buffer, __SIZE_TYPE__ size) {
    (void) name; (void) buffer; (void) size; return "user.example";
}
static int vchroot_expand(struct vchroot_expand_args* args) { (void) args; return 0; }
static int get_perthread_wd(void) { return LINUX_AT_FDCWD; }
static int atfd(int fd) { (void) fd; return LINUX_AT_FDCWD; }
static int do_linux_fchmodat(int fd, const char* path, int mode, int flag) {
    (void) fd; (void) path; (void) mode; (void) flag; ++sys_calls; return syscall_result;
}
#define LINUX_SYSCALL(nr, ...) (++sys_calls, syscall_result)
#define LINUX_SYSCALL2(nr, ...) (++sys_calls, syscall_result)

static void reset(int result) {
    sip_calls = 0;
    sys_calls = 0;
    sip_result = 0;
    syscall_result = result;
}
'''


@unittest.skipUnless(shutil.which('cc') and shutil.which('patch'), 'C compiler and patch required')
class M7SipGates(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def patched(self, path):
        return sources.CompatibilityChecks.patched(self, 'xnu', path)

    def body(self, path, name):
        return sources.function(self.patched(path), name)

    def generic_body(self, name, has_path):
        source = self.patched(BASE + 'include/xnu_syscall/bsd/helper/xattr/setattrlist_generic.c')
        body = source[source.index('long\nFUNC_NAME('):]
        body = body.replace('FUNC_NAME', name)
        return '#define HAS_PATH %d\n' % has_path + body

    def test_patch_gates_every_m7_wrapper(self):
        patch = (ROOT / 'patches/xnu.patch').read_text()
        for path, check in GATED.items():
            marker = 'diff --git a/' + path + ' b/' + path + '\n'
            self.assertIn(marker, patch, path + ' is not patched')
            section = patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]
            added = [line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++')]
            self.assertTrue(any(check in line for line in added),
                            path + ' never calls ' + check)

    def test_xattr_wrappers_refuse_protected_paths(self):
        self.run_c(STUBS + self.body(GATED_PATH_REMOVEXATTR, 'sys_removexattr')
                   + self.body(GATED_FREMOVEXATTR, 'sys_fremovexattr') + r'''
int main(void) {
    reset(0);
    sip_result = -EPERM;
    assert(sys_removexattr("/System/Library/x", "com.apple.rootless", 0) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);
    reset(0);
    sip_result = -EPERM;
    assert(sys_fremovexattr(3, "com.apple.rootless", 0) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);

    reset(0);
    assert(sys_removexattr("/Users/zero/x", "user.example", 0) == 0);
    assert(sip_calls == 1 && sys_calls == 1);
    reset(0);
    assert(sys_fremovexattr(3, "user.example", 0) == 0);
    assert(sip_calls == 1 && sys_calls == 1);
}
''')

    def test_mode_and_node_wrappers_refuse_protected_paths(self):
        self.run_c(STUBS + self.body(GATED_CHMOD_EXTENDED, 'sys_chmod_extended')
                   + self.body(GATED_FCHMOD_EXTENDED, 'sys_fchmod_extended')
                   + self.body(GATED_MKFIFO, 'sys_mkfifo')
                   + self.body(GATED_MKNOD, 'sys_mknod') + r'''
int main(void) {
    reset(0);
    sip_result = -EPERM;
    assert(sys_chmod_extended("/System/Library/x", -1, -1, 0777, NULL) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);
    reset(0);
    sip_result = -EPERM;
    assert(sys_fchmod_extended(3, -1, -1, 0777, NULL) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);
    reset(0);
    sip_result = -EPERM;
    assert(sys_mkfifo("/System/Library/fifo", 0600) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);
    reset(0);
    sip_result = -EPERM;
    assert(sys_mknod("/System/Library/node", 0600, 0) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);

    /* The no-op fchmod_extended call stays a no-op and never touches SIP. */
    reset(0);
    assert(sys_fchmod_extended(3, KAUTH_UID_NONE, KAUTH_GID_NONE, -1, NULL) == 0);
    assert(sip_calls == 0 && sys_calls == 0);

    reset(0);
    assert(sys_chmod_extended("/Users/zero/x", 501, 20, 0644, NULL) == 0);
    assert(sip_calls == 1 && sys_calls == 2);
    reset(0);
    assert(sys_mkfifo("/Users/zero/fifo", 0600) == 0);
    assert(sip_calls == 1 && sys_calls == 1);
}
''')

    def test_setattrlist_refuses_protected_paths_and_fds(self):
        common = STUBS + r'''
#define ATTR_BIT_MAP_COUNT 5
#define ATTR_CMN_CRTIME 0x200
#define ATTR_CMN_MODTIME 0x400
#define ATTR_CMN_CHGTIME 0x800
#define ATTR_CMN_ACCTIME 0x1000
#define ATTR_CMN_ACCESSMASK 0x20000
#define COMMON_SUPPORTED (ATTR_CMN_MODTIME|ATTR_CMN_ACCTIME|ATTR_CMN_ACCESSMASK)
#define VOLUME_SUPPORTED 0
#define DIR_SUPPORTED 0
#define FILE_SUPPORTED 0
#define FORK_SUPPORTED 0
#define FSOPT_NOFOLLOW 1
'''
        self.run_c(common + self.generic_body('sys_setattrlistat', 1) + r'''
int main(void) {
    struct xnu_attrlist list;
    int perms = 0777;
    memset(&list, 0, sizeof(list));
    list.bitmapcount = 5;
    list.commonattr = ATTR_CMN_ACCESSMASK;

    reset(0);
    sip_result = -EPERM;
    assert(sys_setattrlistat(LINUX_AT_FDCWD, "/System/Library/x", &list, &perms, sizeof(perms), 0) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);

    reset(0);
    assert(sys_setattrlistat(LINUX_AT_FDCWD, "/Users/zero/x", &list, &perms, sizeof(perms), 0) == 0);
    assert(sip_calls == 1 && sys_calls == 1);

    /* A request with no mutable attribute is a read-only no-op. */
    reset(0);
    list.commonattr = 0;
    assert(sys_setattrlistat(LINUX_AT_FDCWD, "/System/Library/x", &list, &perms, sizeof(perms), 0) == 0);
    assert(sip_calls == 0 && sys_calls == 0);
}
''')
        self.run_c(common + self.generic_body('sys_fsetattrlist', 0) + r'''
int main(void) {
    struct xnu_attrlist list;
    int perms = 0777;
    memset(&list, 0, sizeof(list));
    list.bitmapcount = 5;
    list.commonattr = ATTR_CMN_ACCESSMASK;

    reset(0);
    sip_result = -EPERM;
    assert(sys_fsetattrlist(3, &list, &perms, sizeof(perms), 0) == -EPERM);
    assert(sip_calls == 1 && sys_calls == 0);

    reset(0);
    assert(sys_fsetattrlist(3, &list, &perms, sizeof(perms), 0) == 0);
    assert(sip_calls == 1 && sys_calls == 1);
}
''')


GATED_PATH_REMOVEXATTR = BASE + 'src/xnu_syscall/bsd/impl/xattr/removexattr.c'
GATED_FREMOVEXATTR = BASE + 'src/xnu_syscall/bsd/impl/xattr/fremovexattr.c'
GATED_CHMOD_EXTENDED = BASE + 'src/xnu_syscall/bsd/impl/unistd/chmod_extended.c'
GATED_FCHMOD_EXTENDED = BASE + 'src/xnu_syscall/bsd/impl/unistd/fchmod_extended.c'
GATED_MKFIFO = BASE + 'src/xnu_syscall/bsd/impl/stat/mkfifo.c'
GATED_MKNOD = BASE + 'src/xnu_syscall/bsd/impl/unistd/mknod.c'

if __name__ == '__main__':
    unittest.main()
