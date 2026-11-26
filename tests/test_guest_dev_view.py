"""The guest /dev lists macOS devfs names instead of the whole host /dev.

The base layer used to install dev -> /Volumes/SystemRoot/dev, so every guest
listing and open() saw the host's GPU, input, disk and kvm nodes. It is now a
directory of placeholders named after the macOS 15.5 devfs entries Darling can
back (null, zero, random, urandom, tty, ptmx, console, fd, stdin/stdout/stderr
-> fd/N), and vchroot resolves those names, plus the unlisted pts and shm that
Darling's pseudo-terminals and shared-memory helpers address by path, to the
host nodes. Host ELF libraries (GPU drivers, PipeWire) open host paths through
glibc and never pass through vchroot.

Source-only and host-only: these tests never start Darling.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
VCHROOT = 'darling/src/libsystem_kernel/emulation/src/linux_premigration/vchroot_userspace.c'
EMULATION = ROOT / 'build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation'
LISTED = ('null', 'zero', 'random', 'urandom', 'tty', 'ptmx', 'console')


def section(patch, path):
    text = (ROOT / 'patches' / patch).read_text()
    start = text.index('diff --git a/' + path + ' b/' + path + '\n')
    end = text.find('\ndiff --git ', start + 1)
    return text[start:end if end != -1 else len(text)]


def added_lines(patch, path):
    return [line[1:] for line in section(patch, path).splitlines()
            if line.startswith('+') and not line.startswith('+++')]


def redirect_block():
    lines = added_lines('xnu.patch', VCHROOT)
    return '\n'.join(lines[lines.index('// BEGIN guest /dev view'):
                           lines.index('// END guest /dev view') + 1])


class PatchedSources(unittest.TestCase):
    def test_base_layer_installs_a_directory_not_the_host_dev(self):
        added = added_lines('darling.patch', 'CMakeLists.txt')
        removed = [line[1:] for line in section('darling.patch', 'CMakeLists.txt').splitlines()
                   if line.startswith('-') and not line.startswith('---')]
        self.assertIn('InstallSymlink(/Volumes/SystemRoot/dev ${CMAKE_INSTALL_PREFIX}/libexec/darling/dev)',
                      removed)
        self.assertIn('foreach(_guest_dev_name null zero random urandom tty ptmx console)', added)
        self.assertIn('foreach(_guest_dev_link stdin:0 stdout:1 stderr:2)', added)
        self.assertIn('install(DIRECTORY "${DARLING_GUEST_DEV}/" DESTINATION libexec/darling/dev', added)
        # An existing install has dev as a symlink; the install replaces it.
        self.assertIn('\tif(IS_SYMLINK \\"\\${_darling_guest_dev}\\")', added)
        # /var/run/syslog keeps reaching the host socket now that /dev/log is gone.
        self.assertIn('InstallSymlink(/Volumes/SystemRoot/dev/log '
                      '${CMAKE_INSTALL_PREFIX}/libexec/darling/var/run/syslog)', added)

    def test_vchroot_redirects_before_the_component_is_examined(self):
        added = added_lines('xnu.patch', VCHROOT)
        self.assertIn('\t\t\t\tint prevlen = ctxt->current_path_len;', added)
        self.assertIn('\t\t\t\tguest_dev_redirect(ctxt, &prevlen);', added)
        block = redirect_block()
        for name in LISTED + ('fd', 'pts', 'shm'):
            self.assertIn('{ "%s", ' % name, block)
        self.assertIn('{ "fd", "/proc/self/fd" },', block)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class RedirectFunction(unittest.TestCase):
    """guest_dev_redirect() as resplices leave it, on path strings only."""

    def test_only_listed_names_directly_under_the_prefix_dev_redirect(self):
        harness = textwrap.dedent('''
            #include <stdbool.h>
            #include <stdio.h>
            #include <string.h>
            struct context { const char* current_root; int current_root_len;
                char current_path[4096]; int current_path_len; };
            %s
            static const char prefix[] = "/home/u/.darling";
            static void check(const char* path, const char* name, int expect, const char* want) {
                struct context c = { .current_root = prefix, .current_root_len = (int) strlen(prefix) };
                snprintf(c.current_path, sizeof c.current_path, "%%s%%s%%s", prefix, path, name);
                c.current_path_len = (int) strlen(c.current_path);
                int prevlen = (int) (strlen(prefix) + strlen(path));
                bool got = guest_dev_redirect(&c, &prevlen);
                if (got != expect || strcmp(c.current_path, want) != 0) {
                    printf("FAIL %%s%%s -> %%d %%s\\n", path, name, got, c.current_path); return; }
                if (got && (c.current_root_len != 0 || c.current_path[prevlen - 1] != '/'
                            || strcmp(c.current_path + prevlen, strrchr(want, '/') + 1) != 0)) {
                    printf("FAIL state %%s%%s\\n", path, name); return; }
                printf("ok %%s%%s\\n", path, name);
            }
            int main(void) {
                check("/dev/", "null", 1, "/dev/null");
                check("/dev/", "urandom", 1, "/dev/urandom");
                check("/dev/", "ptmx", 1, "/dev/ptmx");
                check("/dev/", "fd", 1, "/proc/self/fd");
                check("/dev/", "pts", 1, "/dev/pts");
                check("/dev/", "shm", 1, "/dev/shm");
                check("/dev/", "nvidia0", 0, "/home/u/.darling/dev/nvidia0");
                check("/dev/", "dri", 0, "/home/u/.darling/dev/dri");
                check("/dev/", "kvm", 0, "/home/u/.darling/dev/kvm");
                check("/dev/", "NULL", 0, "/home/u/.darling/dev/NULL");
                check("/dev/", "nullx", 0, "/home/u/.darling/dev/nullx");
                check("/Users/u/dev/", "null", 0, "/home/u/.darling/Users/u/dev/null");
                check("/devx/", "null", 0, "/home/u/.darling/devx/null");
                struct context host = { .current_root = "", .current_root_len = 0 };
                strcpy(host.current_path, "/dev/nvidia0");
                host.current_path_len = 12;
                int p = 5;
                puts(guest_dev_redirect(&host, &p) ? "FAIL host" : "ok host");
                puts(guest_dev_host_path("/dev/null") && guest_dev_host_path("/dev/pts/3")
                     && !guest_dev_host_path("/dev/nvidia0") && !guest_dev_host_path("/proc/self/fd")
                     ? "ok reverse" : "FAIL reverse");
                return 0;
            }
        ''') % redirect_block()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'redirect.c')
            source.write_text(harness)
            binary = Path(tmp, 'redirect')
            subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True)
            out = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
        self.assertNotIn('FAIL', out)
        self.assertEqual(out.count('ok '), 15)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
@unittest.skipUnless((EMULATION / 'src/linux_premigration/vchroot_userspace.c').is_file()
                     and 'guest_dev_redirect' in (EMULATION / 'src/linux_premigration/vchroot_userspace.c')
                     .read_text(errors='replace'), 'prepared Darling checkout with the guest /dev view required')
class VchrootExpansion(unittest.TestCase):
    """The whole vchroot path walk, built for the host, against a temporary prefix."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / 'inc/darling').mkdir(parents=True)
        (tmp / 'inc/darling/emulation').symlink_to(EMULATION / 'include')
        harness = tmp / 'harness.c'
        harness.write_text(textwrap.dedent('''
            #define _GNU_SOURCE
            #include <stdint.h>
            #include <stddef.h>
            #include <locale.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>
            #include <unistd.h>
            #include <fcntl.h>
            static locale_t c_locale(void) {
                static locale_t l; if (!l) l = newlocale(LC_ALL_MASK, "C", (locale_t) 0); return l; }
            #define LC_C_LOCALE c_locale()
            #define __simple_printf printf
            #define __simple_abort abort
            static int dserver_rpc_vchroot_path(char* b, size_t s, uint64_t* l) { (void) b; (void) s; (void) l; return -1; }
            #define main vchroot_test_main
            #include "%s"
            #undef main
            int main(int argc, char** argv) {
                int dfd = open(argv[1], O_RDONLY | O_DIRECTORY);
                __darling_vchroot(dfd);
                close(dfd);
                for (int i = 2; i < argc; i++) {
                    struct vchroot_expand_args a = { .dfd = -100, .flags = 0 };
                    const char* p = argv[i];
                    if (*p == '!') { a.flags = VCHROOT_FOLLOW; p++; }
                    strcpy(a.path, p);
                    int rv = vchroot_expand(&a);
                    char fdpath[4096] = "-";
                    struct vchroot_unexpand_args u;
                    strcpy(u.path, a.path);
                    vchroot_unexpand(&u);
                    int fd = open(a.path, O_RDONLY | O_NONBLOCK | O_NOCTTY);
                    if (fd >= 0) {
                        struct vchroot_fdpath_args f = { .fd = fd, .path = fdpath, .maxlen = sizeof fdpath };
                        if (vchroot_fdpath(&f) != 0) strcpy(fdpath, "?");
                        close(fd);
                    }
                    printf("%%s\\t%%d\\t%%s\\t%%s\\t%%s\\n", argv[i], rv, a.path, u.path, fdpath);
                }
                return 0;
            }
        ''') % (EMULATION / 'src/linux_premigration/vchroot_userspace.c'))
        cls.binary = tmp / 'vchroot'
        subprocess.run(['cc', '-DTEST', '-I', str(tmp / 'inc'), '-w', str(harness), '-o', str(cls.binary)],
                       check=True)
        # A prefix with the installed layout, and one with the legacy symlink.
        cls.prefix = tmp / 'prefix'
        dev = cls.prefix / 'dev'
        (dev / 'fd').mkdir(parents=True)
        for name in LISTED:
            (dev / name).touch()
        for name, fd in (('stdin', 0), ('stdout', 1), ('stderr', 2)):
            (dev / name).symlink_to('fd/%d' % fd)
        cls.legacy = tmp / 'legacy'
        cls.legacy.mkdir()
        (cls.legacy / 'dev').symlink_to('/Volumes/SystemRoot/dev')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def expand(self, prefix, *paths, cwd=None):
        out = subprocess.run([str(self.binary), str(prefix), *paths], check=True, capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, cwd=cwd).stdout
        self.guest = {}
        result = {}
        for line in out.splitlines():
            if line.count('\t') == 4:
                path, rv, expanded, unexpanded, fdpath = line.split('\t')
                result[path] = expanded
                self.guest[path] = (unexpanded, fdpath)
        return result

    def test_macos_names_reach_the_host_nodes(self):
        got = self.expand(self.prefix, '!/dev/null', '/dev/null', '!/dev/urandom', '!/dev/tty',
                          '!/dev/ptmx', '/dev/fd', '!/dev/stdin', '!/dev/fd/0', '/dev/../dev/zero',
                          '!/Volumes/SystemRoot/dev/null')
        self.assertEqual(got['!/dev/null'], '/dev/null')
        self.assertEqual(got['/dev/null'], '/dev/null')
        self.assertEqual(got['!/dev/urandom'], '/dev/urandom')
        self.assertEqual(got['!/dev/tty'], '/dev/tty')
        self.assertEqual(got['/dev/fd'], '/proc/self/fd')
        # stdin -> fd/0 -> /proc/self/fd/0 -> the harness's stdin, /dev/null.
        self.assertEqual(got['!/dev/stdin'], '/dev/null')
        self.assertEqual(got['!/dev/fd/0'], '/dev/null')
        self.assertEqual(got['/dev/../dev/zero'], '/dev/zero')
        self.assertEqual(got['!/Volumes/SystemRoot/dev/null'], '/dev/null')
        # ptmx must stay next to the host devpts (kernel looks up its pts sibling).
        self.assertTrue(got['!/dev/ptmx'] in ('/dev/ptmx', '/dev/pts/ptmx'), got['!/dev/ptmx'])

    def test_host_nodes_read_back_as_guest_dev_paths(self):
        self.expand(self.prefix, '!/dev/null', '!/dev/zero', '!/dev/urandom', '/dev/shm/x', '/Volumes/SystemRoot/etc')
        self.assertEqual(self.guest['!/dev/null'], ('/dev/null', '/dev/null'))
        self.assertEqual(self.guest['!/dev/zero'], ('/dev/zero', '/dev/zero'))
        self.assertEqual(self.guest['!/dev/urandom'], ('/dev/urandom', '/dev/urandom'))
        self.assertEqual(self.guest['/dev/shm/x'][0], '/dev/shm/x')
        # Other host paths keep the SystemRoot volume prefix.
        self.assertEqual(self.guest['/Volumes/SystemRoot/etc'], ('/Volumes/SystemRoot/etc',) * 2)
        # A placeholder in the prefix reads back as its guest path too.
        self.expand(self.prefix, '/dev')
        self.assertEqual(self.guest['/dev'], ('/dev', '/dev'))

    def test_unlisted_pts_and_shm_stay_reachable(self):
        got = self.expand(self.prefix, '/dev/shm/darling-test', '/dev/pts/0', '/dev/shm')
        self.assertEqual(got['/dev/shm/darling-test'], '/dev/shm/darling-test')
        self.assertEqual(got['/dev/pts/0'], '/dev/pts/0')
        self.assertEqual(got['/dev/shm'], '/dev/shm')

    def test_linux_only_devices_have_no_guest_path(self):
        real = os.path.realpath(self.prefix)
        got = self.expand(self.prefix, '!/dev/nvidia0', '!/dev/dri/renderD128', '!/dev/kvm',
                          '!/dev/input/event0', '!/dev/sda', '!/dev/log')
        for path, expanded in got.items():
            self.assertEqual(expanded, real + path[1:], path)

    def test_relative_lookups_from_the_guest_dev(self):
        got = self.expand(self.prefix, 'null', 'nvidia0', cwd=self.prefix / 'dev')
        self.assertEqual(got['null'], '/dev/null')
        self.assertEqual(got['nvidia0'], os.path.realpath(self.prefix) + '/dev/nvidia0')

    def test_legacy_prefix_layout_still_resolves(self):
        got = self.expand(self.legacy, '!/dev/null', '/dev/shm/x')
        self.assertEqual(got['!/dev/null'], '/dev/null')
        self.assertEqual(got['/dev/shm/x'], '/dev/shm/x')


@unittest.skipUnless(shutil.which('cmake'), 'cmake required')
class BaseLayerInstall(unittest.TestCase):
    """The CMake block from darling.patch, installed over the legacy symlink."""

    def test_install_replaces_the_legacy_symlink_with_the_macos_listing(self):
        added = added_lines('darling.patch', 'CMakeLists.txt')
        begin = added.index('# Guest /dev lists the macOS devfs names Darling backs instead of the whole')
        end = next(i for i in range(begin, len(added)) if added[i].startswith('\tFILE_PERMISSIONS'))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'src').mkdir()
            (tmp / 'src/CMakeLists.txt').write_text(
                'cmake_minimum_required(VERSION 3.13)\nproject(guest_dev NONE)\n'
                + '\n'.join(added[begin:end + 1]) + '\n')
            dest = tmp / 'dest'
            sdk = dest / 'opt/libexec/darling'
            sdk.mkdir(parents=True)
            (sdk / 'dev').symlink_to('/Volumes/SystemRoot/dev')
            subprocess.run(['cmake', '-S', str(tmp / 'src'), '-B', str(tmp / 'b'), '-DCMAKE_INSTALL_PREFIX=/opt'],
                           check=True, capture_output=True)
            env = dict(os.environ, DESTDIR=str(dest))
            for _ in range(2):  # a reinstall over the directory works too
                subprocess.run(['cmake', '--install', str(tmp / 'b')], check=True, capture_output=True, env=env)
            dev = sdk / 'dev'
            self.assertFalse(dev.is_symlink())
            self.assertEqual(sorted(p.name for p in dev.iterdir()),
                             sorted(LISTED + ('fd', 'stdin', 'stdout', 'stderr')))
            for name, fd in (('stdin', 0), ('stdout', 1), ('stderr', 2)):
                self.assertEqual(os.readlink(dev / name), 'fd/%d' % fd)
            self.assertTrue((dev / 'fd').is_dir())
            self.assertEqual((dev / 'null').stat().st_mode & 0o777, 0o444)


if __name__ == '__main__':
    unittest.main()
