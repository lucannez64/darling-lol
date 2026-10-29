"""Compile and exercise the security policies without installing a guest runtime."""
from pathlib import Path
import os
import json
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def added_file(patch, path):
    sections = (ROOT / patch).read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++')) + '\n'


def function(source, name):
    match = re.search(r'^([ \t]*)[^\n]*\b' + name + r'\([^\n]*\)\s*\{[\s\S]*?^\1\}', source, re.M)
    if not match:
        raise AssertionError('missing policy function ' + name)
    return match.group()


class SecurityHostTests(unittest.TestCase):
    def compile_run(self, source, flags=()):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / 'check.c'
            binary = folder / 'check'
            path.write_text(source)
            built = subprocess.run(['cc', '-std=gnu11', '-I' + str(ROOT / 'tools/dc-codesign'),
                            str(path), '-o', str(binary), *flags], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            return subprocess.run([str(binary)], capture_output=True, text=True)

    def test_policy_helpers_are_identical_in_both_runtime_consumers(self):
        copies = {
            'dc_entitlements.h': [('patches/darling.patch', 'src/startup/mldr/include/dc_entitlements.h'),
                                  ('patches/darlingserver.patch', 'include/darlingserver/dc_entitlements.h')],
            'dc_codesign.c': [('patches/darling.patch', 'src/startup/mldr/dc_codesign.c'),
                              ('patches/darlingserver.patch', 'src/dc_codesign.c')],
            'dc_codesign.h': [('patches/darling.patch', 'src/startup/mldr/dc_codesign.h'),
                              ('patches/darlingserver.patch', 'src/dc_codesign.h')],
            'dc_image_snapshot.h': [('patches/darling.patch', 'src/startup/mldr/include/dc_image_snapshot.h'),
                                     ('patches/xnu.patch', 'darling/src/libsystem_kernel/emulation/include/common/dc_image_snapshot.h'),
                                     ('patches/dyld.patch', 'src/dc_image_snapshot.h')],
            'dc_mseal.h': [('patches/darling.patch', 'src/startup/mldr/include/dc_mseal.h'),
                          ('patches/xnu.patch', 'darling/src/libsystem_kernel/emulation/include/common/dc_mseal.h')],
        }
        for name, locations in copies.items():
            expected = (ROOT / 'tools/dc-codesign' / name).read_text()
            for patch, path in locations:
                with self.subTest(path=path):
                    self.assertEqual(added_file(patch, path), expected)

    def test_plist_policy(self):
        flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'libxml-2.0'], text=True).split()
        result = self.compile_run((ROOT / 'tests/security/host_entitlements.c').read_text(), flags)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'Darling x86-64 Linux syscall ABI')
    def test_sealed_pages_do_not_follow_the_backing_file(self):
        result = self.compile_run((ROOT / 'tests/security/host_mseal.c').read_text())
        if result.returncode == 77:
            self.skipTest(result.stdout.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_verification_and_mapping_can_share_immutable_descriptors(self):
        result = self.compile_run((ROOT / 'tests/security/host_image_snapshot.c').read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_usr_local_hardlink_cannot_modify_a_learned_protected_inode(self):
        source = added_file('patches/xnu.patch',
                            'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/misc/sip.c')
        preamble = r'''
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <errno.h>
#include <assert.h>
#include <stdio.h>
static const char *prefix;
static int dc_sip_enabled(void) { return 1; }
struct vchroot_unexpand_args { char path[4096]; };
static int vchroot_unexpand(struct vchroot_unexpand_args *a) {
 size_t n = strlen(prefix); if (strncmp(a->path, prefix, n)) return -1;
 memmove(a->path, a->path + n, strlen(a->path + n) + 1); return 0;
}
static int sip_stat_inode(const char *path, uint64_t *dev, uint64_t *ino) {
 struct stat st; if (stat(path, &st)) return -1; *dev = st.st_dev; *ino = st.st_ino; return 0;
}
'''
        registry = source[source.index('#define SIP_INODE_CAPACITY'):source.index('#ifdef __NR_newfstatat')]
        code = preamble + '\n'.join(function(source, name) for name in ('has_path', 'path_is', 'normalize_path'))
        code += '\n' + registry + '\n' + function(source, 'sip_remember_path') + '\n' + function(source, 'dc_sip_check_write')
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            (prefix / 'System').mkdir()
            (prefix / 'usr/local').mkdir(parents=True)
            (prefix / 'tmp').mkdir()
            (prefix / 'System/target').write_bytes(b'protected fixture')
            os.link(prefix / 'System/target', prefix / 'usr/local/alias')
            os.link(prefix / 'System/target', prefix / 'tmp/alias')
            code += '\nint main(void) { prefix = ' + json.dumps(str(prefix)) + r''';
 char path[4096];
 const char *names[] = {"/System/target", "/tmp/alias", "/usr/local/alias"};
 for (int i = 0; i < 3; i++) { snprintf(path, sizeof(path), "%s%s", prefix, names[i]); assert(dc_sip_check_write(path) == -EPERM); }
 snprintf(path, sizeof(path), "%s/usr/local/new-file", prefix); assert(dc_sip_check_write(path) == 0);
 puts("PASS learned inode restrictions survive /usr/local aliases");
}
'''
            result = self.compile_run(code)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
