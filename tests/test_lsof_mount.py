"""lsof, mount and umount are installed at their macOS 15.5 paths, and the libproc and
getfsstat answers they print from look like a Mac's.

Source-only: reads patches/darling.patch and patches/xnu.patch and compiles the pure
helpers from the patched C files against small fakes. Never starts Darling.
Measured on macOS 15.5 (24F74): /usr/sbin/lsof (lsof 4.91, "libproc-based"),
/sbin/mount and /sbin/umount; `mount` prints
"/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)".
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DARLING_PATCH = ROOT / 'patches/darling.patch'
XNU_PATCH = ROOT / 'patches/xnu.patch'
EMU = 'darling/src/libsystem_kernel/emulation/'


def patched_file(patch, path):
    """The added and kept lines of path's hunks in patch."""
    text = patch.read_text()
    start = text.index('+++ b/' + path + '\n')
    end = text.find('\ndiff --git ', start)
    section = text[start:end if end != -1 else len(text)]
    return '\n'.join(line[1:] for line in section.splitlines()[1:]
                     if line[:1] in ('+', ' ') and not line.startswith('+++'))


def c_function(source, name):
    """The full definition of C function name in source."""
    match = re.search(r'^(?:static )?[\w ]+?\**\s*\b' + name + r'\([^;]*?\)\n\{\n', source, re.M)
    if not match:
        raise AssertionError(name + ' not found')
    end = source.index('\n}\n', match.start())
    return source[match.start():end + 3]


class InstallLayoutTests(unittest.TestCase):
    def test_tools_are_built_with_the_command_line_component(self):
        cmake = patched_file(DARLING_PATCH, 'src/CMakeLists.txt')
        self.assertIn('\tadd_subdirectory(external/system_cmds)\n\tadd_subdirectory(lsof)\n'
                      '\tadd_subdirectory(diskdev_cmds)\n', cmake)

    def test_lsof_lists_mapped_files_like_apples_build(self):
        # Without PROC_PIDREGIONPATHINFO2 lsof falls back to flavor 8 and prints no txt rows.
        cmake = patched_file(DARLING_PATCH, 'src/lsof/CMakeLists.txt')
        self.assertIn('target_compile_definitions(lsof PRIVATE PROC_PIDREGIONPATHINFO2=22)', cmake)

    def test_lsof_installs_at_usr_sbin(self):
        cmake = patched_file(DARLING_PATCH, 'src/lsof/CMakeLists.txt')
        self.assertIn('install(TARGETS lsof DESTINATION libexec/darling/usr/sbin)', cmake)
        self.assertIn('DARWINV=2100', cmake)
        for source in ('dproc.c', 'dfile.c', 'dsock.c', 'main.c', 'lib/ptti.c'):
            self.assertIn('\t' + source + '\n', cmake)

    def test_mount_and_umount_install_at_sbin(self):
        cmake = patched_file(DARLING_PATCH, 'src/diskdev_cmds/CMakeLists.txt')
        self.assertIn('install(TARGETS mount umount DESTINATION libexec/darling/sbin)', cmake)
        self.assertIn('add_darling_executable(mount\n', cmake)
        self.assertIn('add_darling_executable(umount\n', cmake)

    def test_vendored_sources_keep_their_licenses(self):
        lsof_license = patched_file(DARLING_PATCH, 'src/lsof/LICENSE')
        self.assertIn('Purdue Research Foundation', lsof_license)
        self.assertIn('4. This notice may not be removed or altered.', lsof_license)
        self.assertIn('Copyright 1994 Purdue Research Foundation',
                      patched_file(DARLING_PATCH, 'src/lsof/main.c'))
        self.assertIn('APPLE PUBLIC SOURCE LICENSE',
                      patched_file(DARLING_PATCH, 'src/diskdev_cmds/APPLE_LICENSE'))
        self.assertIn('@APPLE_LICENSE_HEADER_START@',
                      patched_file(DARLING_PATCH, 'src/diskdev_cmds/mount.tproj/mount.c'))

    def test_lsof_reports_itself_like_apples_build(self):
        version = patched_file(DARLING_PATCH, 'src/lsof/version.h')
        self.assertIn('#define\tLSOF_VERSION\t"4.91"', version)
        self.assertIn('#define\tLSOF_CINFO\t"libproc-based"', version)
        # Apple's build disables the kernel identity check.
        self.assertIn('/* #define\tHASKERNIDCK\t1 */', patched_file(DARLING_PATCH, 'src/lsof/machine.h'))

    def test_mount_prints_with_apples_prmount(self):
        mount = patched_file(DARLING_PATCH, 'src/diskdev_cmds/mount.tproj/mount.c')
        self.assertIn('printf("%s on %s (%s", sfp->f_mntfromname, sfp->f_mntonname, sfp->f_fstypename);',
                      mount)
        flags = patched_file(DARLING_PATCH, 'src/diskdev_cmds/mount_flags_dir/mount_flags.c')
        self.assertIn('{ MNT_RDONLY,       "read-only",    "rdonly",       },', flags)


class ProcInfoRoutingTests(unittest.TestCase):
    def setUp(self):
        self.info = patched_file(XNU_PATCH, EMU + 'src/xnu_syscall/bsd/impl/misc/proc_info.c')

    def test_lsof_flavors_are_answered(self):
        self.assertIn('return darling_proc_pidfdinfo(pid, (int32_t) arg, flavor, buffer, bufsize);',
                      self.info)
        self.assertIn('case PROC_PIDVNODEPATHINFO:', self.info)
        self.assertIn('return darling_proc_pidregionpathinfo2(pid, arg, buffer, bufsize);', self.info)
        # lsof loops forever on a failing PROC_PIDLISTFILEPORTS; no fileports is 0 bytes.
        self.assertRegex(self.info, r'case PROC_PIDLISTFILEPORTS:\n\t\t\{\n(?:\t\t\t//.*\n)?\t\t\treturn 0;')
        # lsof sizes its PROC_PIDLISTFDS buffer from pbi_nfiles.
        self.assertIn('info->pbi_nfiles = darling_proc_nfiles(pid);', self.info)

    def test_other_processes_are_read_from_the_guest_procfs(self):
        # Guest pids live in their own PID namespace, whose procfs darlingserver mounts at
        # <prefix>/proc; the /proc a raw system call reaches is the outer namespace's.
        vchroot = patched_file(XNU_PATCH, EMU + 'src/linux_premigration/vchroot_userspace.c')
        self.assertIn('int vchroot_guest_proc(const char* path, char* out, unsigned int size)', vchroot)
        fdinfo = patched_file(XNU_PATCH, EMU + 'src/xnu_syscall/bsd/impl/misc/proc_fdinfo.c')
        self.assertEqual(fdinfo.count('vchroot_guest_proc(path, full, sizeof(full))'), 2)
        self.assertEqual(fdinfo.count('LINUX_SYSCALL(__NR_open, '), 1)
        self.assertIn('dir = open_linux_flags(path, 0200000 /* O_DIRECTORY */);', fdinfo)
        self.assertIn('if (starts_with(linux_path, "/proc/")) {\n\t\tif (vchroot_guest_proc(linux_path, full, sizeof(full)) < 0)', fdinfo)
        self.assertNotIn('LINUX_SYSCALL(__NR_access, path, 0)', fdinfo)
        info = patched_file(XNU_PATCH, EMU + 'src/xnu_syscall/bsd/impl/misc/proc_info.c')
        self.assertIn('dir = vchroot_guest_proc(path, full, sizeof(full));', info)

    def test_fdinfo_is_built(self):
        cmake = patched_file(XNU_PATCH, EMU + 'CMakeLists.txt')
        self.assertIn('\tsrc/xnu_syscall/bsd/impl/misc/proc_fdinfo.c\n', cmake)

    def test_fdinfo_only_describes_visible_descriptors_at_full_size(self):
        fdinfo = patched_file(XNU_PATCH, EMU + 'src/xnu_syscall/bsd/impl/misc/proc_fdinfo.c')
        self.assertIn('type = darling_fd_view_type(pid, fd);', fdinfo)
        for flavor in ('PROC_PIDFDVNODEPATHINFO', 'PROC_PIDFDPIPEINFO', 'PROC_PIDFDSOCKETINFO',
                       'PROC_PIDFDKQUEUEINFO'):
            self.assertIn('case ' + flavor + ':', fdinfo)
        # lsof exits when an answer is shorter than the structure it asked for.
        for struct in ('vnode_fdinfowithpath', 'pipe_fdinfo', 'socket_fdinfo', 'kqueue_fdinfo',
                       'proc_vnodepathinfo', 'proc_regionwithpathinfo'):
            self.assertIn('struct ' + struct + '* info = (struct ' + struct + '*) buffer;', fdinfo)
        self.assertEqual(fdinfo.count('return sizeof(*info);'), 6)
        self.assertIn('return -EINVAL; /* XNU: no further file-backed region */', fdinfo)


HELPER_HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define TSI_S_CLOSED 0
#define TSI_S_LISTEN 1
#define TSI_S_SYN_SENT 2
#define TSI_S_SYN_RECEIVED 3
#define TSI_S_ESTABLISHED 4
#define TSI_S__CLOSE_WAIT 5
#define TSI_S_FIN_WAIT_1 6
#define TSI_S_CLOSING 7
#define TSI_S_LAST_ACK 8
#define TSI_S_FIN_WAIT_2 9
#define TSI_S_TIME_WAIT 10
@FUNCTIONS@
int main(void)
{
	const char* p = " 0100007F:1F90 00000000:0000 0A";
	uint32_t a4 = 0, f4 = 1;
	unsigned char a6[16];
	int port = 0;

	parse_inet_address(&p, false, &a4, a6, &port);
	assert(memcmp(&a4, "\x7f\x00\x00\x01", 4) == 0 && port == 8080);
	parse_inet_address(&p, false, &f4, a6, &port);
	assert(f4 == 0 && port == 0);
	skip_spaces(&p);
	assert(tcp_state_bsd((int) parse_hex(&p)) == TSI_S_LISTEN);
	assert(to_network16(8080) == 0x901F);

	p = "0000000000000000FFFF00000100007F:0050";
	parse_inet_address(&p, true, &a4, a6, &port);
	assert(is_v4_mapped(a6) && a6[12] == 127 && a6[15] == 1 && port == 80);
	assert(tcp_state_bsd(1) == TSI_S_ESTABLISHED && tcp_state_bsd(8) == TSI_S__CLOSE_WAIT);
	assert(tcp_state_bsd(6) == TSI_S_TIME_WAIT && tcp_state_bsd(7) == TSI_S_CLOSED);

	assert(link_inode("socket:[123456]") == 123456 && link_inode("pipe:[42]") == 42);
	assert(link_inode("/Users/u/file") == 0);

	assert(region_is_guest_file("/usr/lib/dyld"));
	assert(region_is_guest_file("/Applications/League of Legends.app/Contents/MacOS/x"));
	assert(!region_is_guest_file("/Volumes/SystemRoot/usr/lib/x86_64-linux-gnu/libc.so.6"));
	assert(!region_is_guest_file("/dev/nvidia0") && !region_is_guest_file("/memfd:x"));
	assert(!region_is_guest_file("[heap]"));
	return 0;
}
'''

MOUNT_HARNESS = r'''
#include <assert.h>
#include <string.h>
#include <stddef.h>
@FUNCTIONS@
int main(void)
{
	/* MNT_* values from <sys/mount.h> */
	enum { RDONLY = 0x1, SYNC = 0x2, NOEXEC = 0x4, NOSUID = 0x8, NODEV = 0x10, LOCAL = 0x1000,
	       NOATIME = 0x10000000 };
	/* ST_VALID (0x20) is set by every Linux statfs; it is not MNT_UNION. */
	assert(statfs_flags_linux_to_bsd(0x20, 0xEF53) == LOCAL);
	assert(statfs_flags_linux_to_bsd(0x20 | 0x1, 0xEF53) == (LOCAL | RDONLY));
	assert(statfs_flags_linux_to_bsd(0x2 | 0x4 | 0x8, 0x01021994) == (NOSUID | NODEV | NOEXEC | LOCAL));
	assert(statfs_flags_linux_to_bsd(0x10 | 0x400, 0x9123683E) == (SYNC | NOATIME | LOCAL));
	assert(statfs_flags_linux_to_bsd(0, 0x6969) == 0); /* NFS is not local */
	assert(statfs_flags_linux_to_bsd(0x1000 /* ST_RELATIME */, 0x794C7630) == LOCAL);

	assert(is_linux_only_fstype("proc") && is_linux_only_fstype("cgroup2"));
	assert(is_linux_only_fstype("sysfs") && is_linux_only_fstype("tracefs"));
	assert(!is_linux_only_fstype("ext4") && !is_linux_only_fstype("overlay"));
	assert(!is_linux_only_fstype("tmpfs") && !is_linux_only_fstype("devfs"));

	/* Only the guest root and the host root are volumes; what is stacked below them is not. */
	char root[] = "/", host[] = "/Volumes/SystemRoot/", tmp[] = "/Volumes/SystemRoot/tmp", proc[] = "/proc";
	char etc[] = "/Volumes/SystemRoot/etc/hosts";
	assert(is_listed_mount_point(root) && is_listed_mount_point(host) && strcmp(host, "/Volumes/SystemRoot") == 0);
	assert(!is_listed_mount_point(tmp) && !is_listed_mount_point(proc) && !is_listed_mount_point(etc));

	char path[] = "/media/My\\040Disk\\134x";
	unescape_mount_field(path);
	assert(strcmp(path, "/media/My Disk\\x") == 0);
	return 0;
}
'''


@unittest.skipUnless(shutil.which('cc'), 'needs a C compiler')
class HelperBehaviourTests(unittest.TestCase):
    def run_harness(self, harness, functions):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'harness.c'
            binary = Path(tmp) / 'harness'
            src.write_text(harness.replace('@FUNCTIONS@', '\n'.join(functions)))
            subprocess.run(['cc', '-std=gnu11', '-w', '-o', str(binary), str(src)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_proc_net_and_maps_parsing(self):
        fdinfo = patched_file(XNU_PATCH, EMU + 'src/xnu_syscall/bsd/impl/misc/proc_fdinfo.c')
        names = ('starts_with', 'parse_hex', 'parse_number', 'skip_spaces', 'parse_inet_address',
                 'tcp_state_bsd', 'is_v4_mapped', 'to_network16', 'link_inode', 'region_is_guest_file')
        self.run_harness(HELPER_HARNESS, [c_function(fdinfo, name) for name in names])

    def test_getfsstat_flags_and_mount_list(self):
        common = patched_file(XNU_PATCH, EMU + 'src/conversion/stat/common.c')
        getfsstat = patched_file(XNU_PATCH, EMU + 'src/xnu_syscall/bsd/impl/stat/getfsstat.c')
        table_start = getfsstat.index('static const char* const linux_only_fstypes[]')
        table = getfsstat[table_start:getfsstat.index('};', table_start) + 2]
        self.run_harness(MOUNT_HARNESS, [
            c_function(common, 'statfs_flags_linux_to_bsd'), table,
            c_function(getfsstat, 'is_linux_only_fstype'), c_function(getfsstat, 'unescape_mount_field'),
            c_function(getfsstat, 'is_listed_mount_point')])

    def test_getfsstat_reports_guest_mount_points(self):
        getfsstat = patched_file(XNU_PATCH, EMU + 'src/xnu_syscall/bsd/impl/stat/getfsstat.c')
        # The root volume comes first, as on macOS.
        self.assertIn('if ((strcmp(uargs.path, "/") == 0) != (pass == 0))', getfsstat)
        self.assertIn('if (vchroot_unexpand(&uargs) != 0)\n\t\t\tcontinue;\n\t\tif (!is_listed_mount_point(uargs.path))',
                      getfsstat)
        self.assertIn('strlcpy(buf[count].f_mntonname, uargs.path, sizeof(buf[count].f_mntonname));',
                      getfsstat)
        common = patched_file(XNU_PATCH, EMU + 'src/conversion/stat/common.c')
        self.assertEqual(common.count('stat->f_flags = statfs_flags_linux_to_bsd(lstat->f_flags, '
                                      'lstat->f_type);'), 2)
        self.assertNotIn('FIXME: convert flags', common)


if __name__ == '__main__':
    unittest.main()
