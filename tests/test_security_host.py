"""Compile and exercise the security policies without installing a guest runtime."""
from pathlib import Path
import os
import json
import re
import hashlib
import struct
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
    match = re.search(r'^([ \t]*)[^\n]*\b' + name + r'\([^\n]*\)\s*(?:const\s*)?\{[\s\S]*?^\1\}', source, re.M)
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
            'dc_macho_slice.h': [('patches/darling.patch', 'src/startup/mldr/include/dc_macho_slice.h')],
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

    def test_universal_architecture_selection(self):
        result = self.compile_run((ROOT / 'tests/security/host_macho_slice.c').read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_gate_verifies_selected_slice_and_keeps_real_flags(self):
        # Minimal synthetic CodeDirectory: one SHA-256 code page, no CMS.
        # This tests slice selection and hashes, not Apple trust policy.
        def signed_image(flags=0):
            identity = b'fixture.universal\0'
            cdsize = 44 + len(identity) + 32
            sigsize = 20 + cdsize
            page = bytearray(4096)
            page[:32] = struct.pack('<8I', 0xfeedfacf, 0x1000007, 3, 2, 1, 16, 0, 0)
            page[32:48] = struct.pack('<4I', 0x1d, 16, 4096, sigsize)
            cd = struct.pack('>9I4BI', 0xfade0c02, cdsize, 0x20001, flags,
                             44 + len(identity), 44, 0, 1, 4096, 32, 2, 0, 12, 0)
            cd += identity + hashlib.sha256(page).digest()
            return bytes(page) + struct.pack('>5I', 0xfade0cc0, sigsize, 1, 0, 20) + cd

        def universal(image, wide=False):
            header = struct.pack('>II', 0xcafebabf if wide else 0xcafebabe, 2)
            if wide:
                header += struct.pack('>IIQQII', 7, 3, 4096, 28, 12, 0)
                header += struct.pack('>IIQQII', 0x1000007, 3, 8192, len(image), 12, 0)
            else:
                header += struct.pack('>5I', 7, 3, 4096, 28, 12)
                header += struct.pack('>5I', 0x1000007, 3, 8192, len(image), 12)
            data = bytearray(8192)
            data[:len(header)] = header
            data[4096:4124] = struct.pack('<7I', 0xfeedface, 7, 3, 2, 0, 0, 0)
            return data + image

        section = next(s for s in (ROOT / 'patches/darling.patch').read_text().split('diff --git ')
                       if s.startswith('a/src/startup/mldr/mldr.c b/'))
        source = '\n'.join(l[1:] for l in section.splitlines() if l.startswith('+') and not l.startswith('+++')) + '\n'
        state = added_file('patches/darlingserver.patch', 'include/darlingserver/cs_state.h')
        # These are the production gate and selection functions; only OS setup
        # and the load_results fields irrelevant to them are omitted.
        selector = re.search(r'^static int select_macho_slice\([\s\S]*?^}', source, re.M).group()
        native = source[source.index('static long dc_native_image_size'):source.index('static long dc_native_allocate')]
        code = r'''
#define _GNU_SOURCE
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
#include "dc_codesign.h"
#include "dc_entitlements.h"
#include "dc_image_snapshot.h"
#include "dc_macho_slice.h"
typedef uint32_t cpu_type_t;
struct load_results { uint32_t bprefs[4]; };
'''
        code += state + '\nstatic struct dc_cs_local_state mldr_cs_state;\n' + native
        code += '\n' + function(source, 'entitlements_key_is_true')
        code += '\n' + function(source, 'flags_from_image') + '\n' + selector
        code += '\n' + function(source, 'codesign_gate')
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            good = signed_image()
            fixtures = {'thin': good, 'fat': universal(good), 'fat64': universal(good, True),
                        'runtime': universal(signed_image(0x10000))}
            bad = bytearray(universal(good)); bad[8192 + 100] ^= 1
            fixtures['tampered'] = bad
            for name, data in fixtures.items():
                (folder / name).write_bytes(data)
            code += '\nint main(void) {\nstruct load_results lr = {{0}}; int fd, r;\nsetenv("DARLING_CODESIGN_ENFORCE", "enforce", 1);\n'
            for name, offset, runtime in [('thin', 0, 0), ('fat', 8192, 0), ('fat64', 8192, 0), ('runtime', 8192, 0x10000)]:
                code += 'r = codesign_gate(' + json.dumps(str(folder / name)) + ', &fd, &lr);\n'
                code += f'assert(r == 0 && fd >= 0); assert(mldr_cs_state.state.image_offset == {offset});\n'
                code += f'assert((mldr_cs_state.state.flags & DC_CS_RUNTIME) == {runtime});\n'
                code += 'assert(mldr_cs_state.state.flags & DC_CS_VALID); assert(mldr_cs_state.state.flags & DC_CS_SIGNED);\n'
                code += 'assert(mldr_cs_state.state.blob_len > 8 && mldr_cs_state.blob[0] == 0xfa);\nclose(fd); free((void*)mldr_cs_state.blob);\n'
            code += 'assert(codesign_gate(' + json.dumps(str(folder / 'tampered')) + ', &fd, &lr) == -1);\n'
            code += 'assert(mldr_cs_state.state.flags == 0);\n'
            # The same container also has an unsigned x86 slice: selecting it
            # must not borrow validity from the signed x86-64 slice.
            code += 'lr.bprefs[0] = DC_CPU_X86;\nassert(codesign_gate(' + json.dumps(str(folder / 'fat')) + ', &fd, &lr) == -1);\n'
            code += 'assert(mldr_cs_state.state.flags == 0);\nputs("PASS selected-slice hashes, offsets, blob copy, real flags and tamper/unsigned rejection");\n}\n'
            flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'libxml-2.0'], text=True).split()
            result = self.compile_run(code, [str(ROOT / 'tools/dc-codesign/dc_codesign.c'), '-lcrypto', *flags])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_csops_uses_authoritative_state_and_commits_only_accepted_updates(self):
        source = added_file('patches/xnu.patch',
                            'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/misc/csops.c')
        code = r'''
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
#include <assert.h>
#include <stdio.h>
#define DC_CS_STATE_VERSION 1
#define DC_CS_VALID 1u
#define DC_CS_REQUIRE_LV 0x2000u
#define DC_CS_FORCED_LV 0x10u
struct dc_cs_state {
 uint32_t version, flags;
 int valid, verified, lv_required;
};
struct dc_cs_local_state { struct dc_cs_state state; };
static struct dc_cs_local_state cached;
static struct dc_cs_state authoritative;
static int query_error, update_error, query_count;
static const struct dc_cs_local_state *local_state(void) { return &cached; }
static int rpc_get_state(int pid, struct dc_cs_state *state,
 void *blob, size_t blob_size, void *entitlements, size_t entitlements_size,
 void *der, size_t der_size) {
 assert(pid == 42); query_count++;
 if (query_error) return query_error;
 *state = authoritative; return 0;
}
static int dserver_rpc_update_cs_state(int pid, uint32_t set, uint32_t clear,
 int valid, uint32_t *flags) {
 assert(pid == 42);
 if (update_error) return update_error;
 authoritative.flags = (authoritative.flags | set) & ~clear;
 if (valid == 0) authoritative.flags &= ~DC_CS_VALID;
 *flags = authoritative.flags; return 0;
}
'''
        code += '\n' + function(source, 'state_for') + '\n' + function(source, 'update_state')
        code += r'''
int main(void) {
 struct dc_cs_state observed;
 cached.state = (struct dc_cs_state){1, DC_CS_VALID, 1, 1, 0};
 authoritative = cached.state;
 /* Another process invalidated this process after the loader cached it. */
 authoritative.flags = 0; authoritative.valid = 0;
 assert(state_for(42, &observed) == 0);
 assert(query_count == 1 && observed.flags == 0 && observed.valid == 0);
 query_error = -ESRCH;
 assert(state_for(42, &observed) == -ESRCH);
 query_error = 0;
 /* A rejected update must leave both the cache and output untouched. */
 struct dc_cs_local_state before = cached;
 uint32_t output = 0xdeadbeef;
 update_error = EPERM;
 assert(update_state(42, 1, DC_CS_REQUIRE_LV, 0, 0, &output) == -EPERM);
 assert(memcmp(&cached, &before, sizeof(cached)) == 0);
 assert(output == 0xdeadbeef);
 update_error = -EIO;
 assert(update_state(42, 1, DC_CS_REQUIRE_LV, 0, 0, &output) == -EIO);
 assert(memcmp(&cached, &before, sizeof(cached)) == 0);
 assert(output == 0xdeadbeef);
 /* On success use the server result, including its earlier invalidation. */
 update_error = 0;
 assert(update_state(42, 1, DC_CS_REQUIRE_LV, 0, -1, &output) == 0);
 assert(output == DC_CS_REQUIRE_LV);
 assert(cached.state.flags == output && cached.state.valid == 0);
 assert(cached.state.lv_required == 1);
 assert(update_state(42, 1, 0, DC_CS_REQUIRE_LV, -1, NULL) == 0);
 assert(cached.state.flags == 0 && cached.state.lv_required == 0);
 /* Updating another process must not overwrite this process's cache. */
 before = cached;
 assert(update_state(42, 0, DC_CS_REQUIRE_LV, 0, -1, NULL) == 0);
 assert(memcmp(&cached, &before, sizeof(cached)) == 0);
 puts("PASS authoritative signing status and atomic cache updates");
}
'''
        result = self.compile_run(code)
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
        code += '\n' + registry + '\n' + function(source, 'sip_remember_path') + '\n' + function(source, 'dc_sip_check_write') + '\n' + function(source, 'dc_sip_check_tree_move')
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
 const char *ancestors[] = {"/private", "/private/var", "/Library", "/"};
 for (int i = 0; i < 4; i++) { snprintf(path, sizeof(path), "%s%s", prefix, ancestors[i]); assert(dc_sip_check_tree_move(path) == -EPERM); }
 snprintf(path, sizeof(path), "%s/usr/local/example", prefix); assert(dc_sip_check_tree_move(path) == 0);
 puts("PASS learned inode restrictions survive /usr/local aliases and protected ancestor moves");
}
'''
            result = self.compile_run(code)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
