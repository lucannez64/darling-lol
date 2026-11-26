"""statfs/fstatfs/getfsstat compat normalization tests.

Under profile=compat, guest paths inside the prefix report macOS APFS/devfs
mount metadata (instead of Linux host filesystem types, block devices and host
mount points from /proc/self/mounts), while preserving real block/inode counts
and leaving /Volumes/SystemRoot and profile=host unchanged.
"""
from pathlib import Path
import shutil
import unittest

import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]
STAT = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/stat'
STATFS = f'{STAT}/statfs.c'
FSTATFS = f'{STAT}/fstatfs.c'
GETFSSTAT = f'{STAT}/getfsstat.c'


def section(target):
    patch = (ROOT / 'patches/xnu.patch').read_text()
    marker = f'diff --git a/{target} b/{target}\n'
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added(target):
    return '\n'.join(line[1:] for line in section(target).splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def compat_statfs_helpers():
    text = added(STATFS)
    start = text.index('static bool lolsys_is_system_root_path(')
    end = text.index('\n\treturn 1;\n}\n', start) + len('\n\treturn 1;\n}\n')
    return text[start:end]


def compat_getfsstat_helper():
    text = added(GETFSSTAT)
    start = text.index('static long lolsys_getfsstat64_compat(')
    end = text.index('\n}\n', start) + 3
    return text[start:end]


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class StatfsCompatTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def test_syscalls_check_compat_profile_and_zero_output(self):
        statfs_added = added(STATFS)
        fstatfs_added = added(FSTATFS)
        getfsstat_added = added(GETFSSTAT)

        self.assertIn('darling_compat_profile_enabled()', statfs_added)
        self.assertIn('vchroot_unexpand(&uargs)', statfs_added)
        self.assertIn('lolsys_apply_compat_statfs64(uargs.path, buf)', statfs_added)
        self.assertIn('__builtin_memset(buf, 0, sizeof(*buf));', statfs_added)

        self.assertIn('darling_compat_profile_enabled() && lolsys_apply_compat_statfs64(path, buf)', fstatfs_added)
        self.assertIn('__builtin_memset(buf, 0, sizeof(*buf));', fstatfs_added)

        self.assertIn('if (darling_compat_profile_enabled())', getfsstat_added)
        self.assertIn('return lolsys_getfsstat64_compat(buf, bufsize);', getfsstat_added)

    def test_compat_statfs_normalizes_guest_paths_and_preserves_counts(self):
        self.run_c(r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define VISIBLE

struct bsd_statfs64 {
	unsigned int f_bsize;
	int f_iosize;
	long long f_blocks;
	long long f_bfree;
	long long f_bavail;
	long long f_files;
	long long f_ffree;
	unsigned long long f_fsid;
	unsigned int f_owner;
	unsigned int f_type;
	unsigned int f_flags;
	unsigned int f_fssubtype;
	char f_fstypename[16];
	char f_mntonname[1024];
	char f_mntfromname[1024];
	long long f_reserved4[4];
};

static size_t test_strlcpy(char* dst, const char* src, size_t size)
{
	size_t len = strlen(src);
	if (size > 0) {
		size_t copy = len >= size ? size - 1 : len;
		memcpy(dst, src, copy);
		dst[copy] = '\0';
	}
	return len;
}
#define strlcpy test_strlcpy

''' + compat_statfs_helpers() + r'''

static struct bsd_statfs64 sample_linux_buf(void)
{
	struct bsd_statfs64 buf;
	memset(&buf, 0, sizeof(buf));
	buf.f_bsize = 4096;
	buf.f_blocks = 123456789LL;
	buf.f_bfree = 54321000LL;
	buf.f_bavail = 50000000LL;
	buf.f_files = 999999LL;
	buf.f_ffree = 888888LL;
	buf.f_fsid = 0x1122334455667788ULL;
	buf.f_type = 0x9123683eu; /* BTRFS_SUPER_MAGIC */
	buf.f_flags = 0x1020u;
	buf.f_owner = 1000;
	buf.f_fssubtype = 7;
	strlcpy(buf.f_fstypename, "btrfs", sizeof(buf.f_fstypename));
	strlcpy(buf.f_mntonname, "/mnt/kingston", sizeof(buf.f_mntonname));
	strlcpy(buf.f_mntfromname, "/dev/sdb2", sizeof(buf.f_mntfromname));
	return buf;
}

int main(void)
{
	struct bsd_statfs64 buf;

	/* Regular guest path -> APFS on /, counts preserved. */
	buf = sample_linux_buf();
	assert(lolsys_apply_compat_statfs64("/Applications/League of Legends.app", &buf) == 1);
	assert(strcmp(buf.f_fstypename, "apfs") == 0);
	assert(strcmp(buf.f_mntonname, "/") == 0);
	assert(strcmp(buf.f_mntfromname, "/dev/disk1s1") == 0);
	assert(buf.f_type == 26u);
	assert(buf.f_flags == 0x0480d000u);
	assert(buf.f_owner == 0u);
	assert(buf.f_fssubtype == 0u);
	assert(buf.f_iosize == 4096);
	assert(buf.f_blocks == 123456789LL);
	assert(buf.f_bfree == 54321000LL);
	assert(buf.f_bavail == 50000000LL);
	assert(buf.f_files == 999999LL);
	assert(buf.f_ffree == 888888LL);
	assert(buf.f_fsid == 0x1122334455667788ULL);

	/* Guest root -> APFS on /. */
	buf = sample_linux_buf();
	assert(lolsys_apply_compat_statfs64("/", &buf) == 1);
	assert(strcmp(buf.f_fstypename, "apfs") == 0);
	assert(strcmp(buf.f_mntonname, "/") == 0);

	/* /dev and /dev/null -> devfs on /dev, while /developer is APFS. */
	buf = sample_linux_buf();
	assert(lolsys_apply_compat_statfs64("/dev", &buf) == 1);
	assert(strcmp(buf.f_fstypename, "devfs") == 0);
	assert(strcmp(buf.f_mntonname, "/dev") == 0);
	assert(strcmp(buf.f_mntfromname, "devfs") == 0);
	assert(buf.f_type == 19u);
	assert(buf.f_flags == 0x04109000u);

	buf = sample_linux_buf();
	assert(lolsys_apply_compat_statfs64("/dev/null", &buf) == 1);
	assert(strcmp(buf.f_fstypename, "devfs") == 0);

	buf = sample_linux_buf();
	assert(lolsys_apply_compat_statfs64("/developer/tools", &buf) == 1);
	assert(strcmp(buf.f_fstypename, "apfs") == 0);

	/* Explicit host-root escape /Volumes/SystemRoot stays untouched. */
	buf = sample_linux_buf();
	assert(lolsys_apply_compat_statfs64("/Volumes/SystemRoot", &buf) == 0);
	assert(lolsys_apply_compat_statfs64("/Volumes/SystemRoot/sys/class/drm", &buf) == 0);
	assert(strcmp(buf.f_fstypename, "btrfs") == 0);

	/* Prefix-only sibling /Volumes/SystemRoot2 is still a guest path. */
	buf = sample_linux_buf();
	assert(lolsys_apply_compat_statfs64("/Volumes/SystemRoot2/file", &buf) == 1);
	assert(strcmp(buf.f_fstypename, "apfs") == 0);

	/* NULL and relative paths are not normalized. */
	assert(lolsys_apply_compat_statfs64(NULL, &buf) == 0);
	assert(lolsys_apply_compat_statfs64("relative/path", &buf) == 0);
	return 0;
}
''')

    def test_compat_getfsstat_returns_two_guest_mounts_and_respects_bufsize(self):
        self.run_c(r'''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <string.h>

struct bsd_statfs64 {
	unsigned int f_bsize;
	int f_iosize;
	long long f_blocks;
	long long f_bfree;
	long long f_bavail;
	long long f_files;
	long long f_ffree;
	unsigned long long f_fsid;
	unsigned int f_owner;
	unsigned int f_type;
	unsigned int f_flags;
	unsigned int f_fssubtype;
	char f_fstypename[16];
	char f_mntonname[1024];
	char f_mntfromname[1024];
	long long f_reserved4[4];
};

static long sys_statfs64(const char* path, struct bsd_statfs64* buf)
{
	memset(buf, 0, sizeof(*buf));
	if (strcmp(path, "/") == 0) {
		strcpy(buf->f_fstypename, "apfs");
		strcpy(buf->f_mntonname, "/");
		strcpy(buf->f_mntfromname, "/dev/disk1s1");
		return 0;
	}
	if (strcmp(path, "/dev") == 0) {
		strcpy(buf->f_fstypename, "devfs");
		strcpy(buf->f_mntonname, "/dev");
		strcpy(buf->f_mntfromname, "devfs");
		return 0;
	}
	return -ENOENT;
}

''' + compat_getfsstat_helper() + r'''

int main(void)
{
	struct bsd_statfs64 mounts[4];

	/* NULL buffer queries the mount count. */
	assert(lolsys_getfsstat64_compat(NULL, 0) == 2);

	/* Negative size is rejected. */
	assert(lolsys_getfsstat64_compat(mounts, -1) == -EINVAL);

	/* Buffer for 1 entry fills only / and leaves slot 1 untouched. */
	memset(mounts, 0x5a, sizeof(mounts));
	assert(lolsys_getfsstat64_compat(mounts, (int) sizeof(struct bsd_statfs64)) == 1);
	assert(strcmp(mounts[0].f_fstypename, "apfs") == 0);
	assert(strcmp(mounts[0].f_mntonname, "/") == 0);
	assert(mounts[1].f_fstypename[0] == 0x5a);

	/* Full buffer fills both / and /dev. */
	memset(mounts, 0, sizeof(mounts));
	assert(lolsys_getfsstat64_compat(mounts, (int) sizeof(mounts)) == 2);
	assert(strcmp(mounts[0].f_fstypename, "apfs") == 0);
	assert(strcmp(mounts[0].f_mntonname, "/") == 0);
	assert(strcmp(mounts[0].f_mntfromname, "/dev/disk1s1") == 0);
	assert(strcmp(mounts[1].f_fstypename, "devfs") == 0);
	assert(strcmp(mounts[1].f_mntonname, "/dev") == 0);
	assert(strcmp(mounts[1].f_mntfromname, "devfs") == 0);
	return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
