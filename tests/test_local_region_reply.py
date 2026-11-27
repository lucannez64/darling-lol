"""The game's in-process mach_vm_region_recurse builds the server's reply.

The League game process answers mach_vm_region_recurse on its own task from
PROCMAP_QUERY without a round trip to darlingserver. The reply has to be the
one the server builds (duct-tape memory.c) for every structure size it accepts:
the short one and the three long ones, which is what the game's own caller
asks for.
"""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TRAPS = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/mach/impl/mach_traps.c'
MEMORY = 'duct-tape/src/memory.c'
SERVER_REPLY_SHA256 = 'b0cbb8d78cda878778db8059b1c28880b8a40d2f17903e491b4714d1316fc02e'


def patched_lines(patch, path):
    text = (ROOT / 'patches' / patch).read_text()
    section = text.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith(('+', ' ')) and not line.startswith('+++'))


class LocalRegionReplyTests(unittest.TestCase):
    def test_local_path_takes_the_counts_the_server_accepts(self):
        traps = patched_lines('xnu.patch', TRAPS)
        entry = traps[traps.index('kern_return_t mach_vm_region_recurse(mach_port_name_t target'):]
        entry = entry[:entry.index('return __darling_rpc_mach_vm_region_recurse(target')]
        # the stub always gets a depth to read
        self.assertIn('return __darling_rpc_mach_vm_region_recurse(target, address, size, '
                      'depth ? depth : &no_depth, info, infoCnt);', traps)
        self.assertIn('*infoCnt == LREGION_SHORT_INFO_COUNT || *infoCnt == LREGION_V0_INFO_COUNT ||', entry)
        self.assertIn('*infoCnt == LREGION_V1_INFO_COUNT || *infoCnt == LREGION_V2_INFO_COUNT', entry)
        # a NULL depth stays answered here: the RPC stub would read it and fault
        self.assertIn('address && size && info && infoCnt &&', entry)
        self.assertIn('if (depth) *depth = 0;', entry)
        self.assertIn('lregion_fill(info, *infoCnt, q.vma_start, q.vma_end, q.vma_flags, q.vma_offset)', entry)
        # no reply of its own for a long count any more
        self.assertNotIn('info[i] = 0', entry)
        # past the last mapping: the server's code, not KERN_NO_SPACE
        self.assertIn('return KERN_INVALID_ADDRESS;', entry)
        self.assertNotIn('KERN_NO_SPACE', entry)

    def test_server_reply_code_is_unchanged(self):
        # lregion_fill() is a copy of this code. Any edit to it changes the digest: carry the
        # edit over to lregion_fill() (and to the cases below), then update the digest.
        server = patched_lines('darlingserver.patch', MEMORY)
        helpers = server[server.index('static vm_prot_t dtape_prot_from_region('):]
        helpers = helpers[:helpers.index('\n}\n', helpers.index('static vm_prot_t dtape_max_prot_from_prot(')) + 3]
        reply = server[server.index('mach_msg_type_number_t requested_cnt = *infoCnt;'):]
        reply = reply[:reply.index('kr = KERN_SUCCESS;')]
        digest = hashlib.sha256((helpers + reply).encode()).hexdigest()
        self.assertEqual(digest, SERVER_REPLY_SHA256,
                         'darlingserver builds a different mach_vm_region_recurse reply: mirror it in lregion_fill()')

    def test_server_reply_is_the_one_mirrored(self):
        # The values lregion_fill() copies, spelled out for the reader of a failure above.
        server = patched_lines('darlingserver.patch', MEMORY)
        recurse = server[server.index('mach_msg_type_number_t requested_cnt = *infoCnt;'):]
        for line in (
            'requested_cnt != VM_REGION_SUBMAP_SHORT_INFO_COUNT_64 &&',
            'requested_cnt != VM_REGION_SUBMAP_INFO_V0_COUNT_64 &&',
            'requested_cnt != VM_REGION_SUBMAP_INFO_V1_COUNT_64 &&',
            'requested_cnt != VM_REGION_SUBMAP_INFO_V2_COUNT_64) {',
            'vm_inherit_t inherit = region_info.shared ? VM_INHERIT_SHARE : VM_INHERIT_COPY;',
            'unsigned char share_mode = region_info.shared ? SM_SHARED : SM_PRIVATE;',
            'vm_object_id_t obj_id = (vm_object_id_t)(region_info.start_address >> 12);',
            'out.pages_resident = (unsigned int)region_info.page_count;',
            'out.ref_count = 1;',
            'out.object_id_full = obj_id;',
            'kr = KERN_INVALID_ADDRESS;',
        ):
            self.assertIn(line, recurse)
        self.assertIn('if ((prot & VM_PROT_EXECUTE) && !(prot & VM_PROT_WRITE)) {\n'
                      '\t\treturn VM_PROT_READ | VM_PROT_EXECUTE;', server)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_reply_for_each_structure_size(self):
        traps = patched_lines('xnu.patch', TRAPS)
        start = traps.index('struct lregion_query {')
        code = traps[start:traps.index('extern kern_return_t __darling_rpc_mach_vm_region_recurse(', start)]
        harness = f"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define VM_PROT_READ 1
#define VM_PROT_WRITE 2
#define VM_PROT_EXECUTE 4
{code}

/* vm_region_submap_info_64 as <mach/vm_region.h> declares it */
#pragma pack(push, 4)
struct long_info {{
    int protection, max_protection;
    unsigned int inheritance;
    unsigned long long offset;
    unsigned int user_tag, pages_resident, pages_shared_now_private, pages_swapped_out, pages_dirtied, ref_count;
    unsigned short shadow_depth;
    unsigned char external_pager, share_mode;
    int is_submap, behavior;
    unsigned int object_id;
    unsigned short user_wired_count;
    unsigned int pages_reusable;
    unsigned long long object_id_full;
}};
struct short_info {{
    int protection, max_protection;
    unsigned int inheritance;
    unsigned long long offset;
    unsigned int user_tag, ref_count;
    unsigned short shadow_depth;
    unsigned char external_pager, share_mode;
    int is_submap, behavior;
    unsigned int object_id;
    unsigned short user_wired_count;
}};
#pragma pack(pop)
_Static_assert(sizeof(struct long_info) == 76 && sizeof(struct short_info) == 48, "Mach ABI");

#define GUARD 0x5a5a5a5a
static int words[24];
static void reset(void) {{ for (int i = 0; i < 24; i++) words[i] = GUARD; }}
static void untouched_from(int count) {{ for (int i = count; i < 24; i++) assert(words[i] == GUARD); }}

int main(void)
{{
    const unsigned long long start = 0x7f1234567000ull, end = start + 5 * 4096;
    struct long_info full;
    struct short_info brief;

    /* the long structure, as the game asks: a private read-write mapping */
    reset();
    assert(lregion_fill(words, 19, start, end, 1 | 2, 0x3000) == 1);
    untouched_from(19);
    memcpy(&full, words, sizeof(full));
    assert(full.protection == (VM_PROT_READ | VM_PROT_WRITE));
    assert(full.max_protection == (VM_PROT_READ | VM_PROT_WRITE));
    assert(full.inheritance == 1 && full.share_mode == 2);    /* VM_INHERIT_COPY, SM_PRIVATE */
    assert(full.offset == 0x3000 && full.pages_resident == 5 && full.ref_count == 1);
    assert(full.is_submap == 0 && full.behavior == 0 && full.user_tag == 0);
    assert(full.object_id == (unsigned int) (start >> 12) && full.object_id_full == start >> 12);
    assert(full.pages_reusable == 0 && full.user_wired_count == 0 && full.external_pager == 0);

    /* read-execute text keeps write out of the maximum; a shared mapping is inherited shared */
    reset();
    assert(lregion_fill(words, 19, start, end, 1 | 4 | 8, 0) == 1);
    memcpy(&full, words, sizeof(full));
    assert(full.protection == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(full.max_protection == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(full.inheritance == 0 && full.share_mode == 4);    /* VM_INHERIT_SHARE, SM_SHARED */

    /* writable and executable; and a mapping with no access at all */
    reset();
    assert(lregion_fill(words, 19, start, end, 1 | 2 | 4, 0) == 1);
    memcpy(&full, words, sizeof(full));
    assert(full.max_protection == (VM_PROT_READ | VM_PROT_WRITE | VM_PROT_EXECUTE));
    reset();
    assert(lregion_fill(words, 19, start, end, 0, 0) == 1);
    memcpy(&full, words, sizeof(full));
    assert(full.protection == 0 && full.max_protection == 0);

    /* the two older long sizes get the same words, cut where they end */
    for (int count = 16; count <= 17; count++) {{
        struct long_info cut;
        reset();
        assert(lregion_fill(words, (unsigned int) count, start, end, 1 | 2, 0x3000) == 1);
        untouched_from(count);
        reset();
        assert(lregion_fill(words, 19, start, end, 1 | 2, 0x3000) == 1);
        memcpy(&full, words, sizeof(full));
        reset();
        assert(lregion_fill(words, (unsigned int) count, start, end, 1 | 2, 0x3000) == 1);
        memset(&cut, 0, sizeof(cut));
        memcpy(&cut, words, (size_t) count * sizeof(int));
        assert(memcmp(&cut, &full, (size_t) count * sizeof(int)) == 0);
    }}

    /* the short structure: its own layout, share_mode at byte 31 */
    reset();
    assert(lregion_fill(words, 12, start, end, 1 | 4, 0x1000) == 1);
    untouched_from(12);
    memcpy(&brief, words, sizeof(brief));
    assert(brief.protection == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(brief.max_protection == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(brief.inheritance == 1 && brief.share_mode == 2 && brief.ref_count == 1);
    assert(brief.offset == 0x1000 && brief.object_id == (unsigned int) (start >> 12));
    assert(((unsigned char*) words)[31] == 2 && ((unsigned char*) words)[47] == 0);

    /* shared, writable-executable and no-access mappings in the short structure */
    reset();
    assert(lregion_fill(words, 12, start, end, 1 | 2 | 8, 0) == 1);
    memcpy(&brief, words, sizeof(brief));
    assert(brief.inheritance == 0 && brief.share_mode == 4);
    assert(brief.max_protection == (VM_PROT_READ | VM_PROT_WRITE));
    reset();
    assert(lregion_fill(words, 12, start, end, 1 | 2 | 4, 0) == 1);
    memcpy(&brief, words, sizeof(brief));
    assert(brief.max_protection == (VM_PROT_READ | VM_PROT_WRITE | VM_PROT_EXECUTE));
    reset();
    assert(lregion_fill(words, 12, start, end, 0, 0) == 1);
    memcpy(&brief, words, sizeof(brief));
    assert(brief.protection == 0 && brief.max_protection == 0 && brief.is_submap == 0 && brief.behavior == 0);

    /* any other count is the server's: nothing is written here (the MIG stub clamps 20 and above to 19) */
    for (unsigned int count = 0; count <= 23; count++) {{
        if (count == 12 || count == 16 || count == 17 || count == 19)
            continue;
        reset();
        assert(lregion_fill(words, count, start, end, 1 | 2, 0) == 0);
        untouched_from(0);
    }}
    puts("ok");
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'region.c'
            binary = Path(folder) / 'region'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                            '-Wno-unused-variable', str(source), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), 'ok')

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_entry_function_on_this_process(self):
        # The whole local path, with real system calls: outputs on success, nothing touched on
        # failure, and what is left to the server (here a stand-in that returns a sentinel).
        traps = patched_lines('xnu.patch', TRAPS)
        start = traps.index('struct lregion_query {')
        code = traps[start:traps.index('kern_return_t semaphore_signal_trap_impl(', start)]
        harness = f"""
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

typedef int kern_return_t;
typedef unsigned int mach_port_name_t;
#define VISIBLE
#define KERN_SUCCESS 0
#define KERN_INVALID_ADDRESS 1
#define VM_PROT_READ 1
#define VM_PROT_WRITE 2
#define VM_PROT_EXECUTE 4
#define LINUX_ENOENT 2
#define SENTINEL 0x7777
static mach_port_name_t mach_task_self(void) {{ return 0x103; }}
static int g_fail_ioctl;
static int darling_is_league_game(void) {{ return 0; }}

/* raw system calls, as the emulation makes them; the switch file that turns the path on exists */
static long fake_syscall(long nr, long a0, long a1, long a2, long a3, long a4, long a5)
{{
    if (nr == SYS_access && strcmp((const char*) a0, "/tmp/rclol-local-region-all") == 0)
        return 0;
    if (nr == SYS_open && strcmp((const char*) a0, "/tmp/rclol-region-pace-us") == 0)
        return -ENOENT;
    if (nr == SYS_ioctl && g_fail_ioctl)
        return -EIO;
    long rc = syscall(nr, a0, a1, a2, a3, a4, a5);
    return rc < 0 ? -errno : rc;
}}
/* every argument converted to long, whatever its type, as the emulation's own macro does */
#define L(x) ((long) (x))
#define SC1(n) fake_syscall(L(n), 0, 0, 0, 0, 0, 0)
#define SC2(n, a) fake_syscall(L(n), L(a), 0, 0, 0, 0, 0)
#define SC3(n, a, b) fake_syscall(L(n), L(a), L(b), 0, 0, 0, 0)
#define SC4(n, a, b, c) fake_syscall(L(n), L(a), L(b), L(c), 0, 0, 0)
#define SC5(n, a, b, c, d) fake_syscall(L(n), L(a), L(b), L(c), L(d), 0, 0)
#define SC_PICK(_1, _2, _3, _4, _5, name, ...) name
#define LINUX_SYSCALL(...) SC_PICK(__VA_ARGS__, SC5, SC4, SC3, SC2, SC1)(__VA_ARGS__)

{code}

static int g_server_calls;
static unsigned int g_server_depth;
kern_return_t __darling_rpc_mach_vm_region_recurse(mach_port_name_t target, unsigned long long* address,
    unsigned long long* size, unsigned int* depth, int* info, unsigned int* infoCnt)
{{
    (void) target; (void) address; (void) size; (void) info; (void) infoCnt;
    g_server_calls++;
    g_server_depth = *depth; /* the generated stub reads it before sending */
    return SENTINEL;
}}

#define GUARD 0x5a5a5a5a
int main(void)
{{
    /* a read-write mapping between two no-access pages, so its bounds are its own */
    char* base = mmap(NULL, 5 * 4096, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(base != MAP_FAILED && mprotect(base + 4096, 3 * 4096, PROT_READ | PROT_WRITE) == 0);
    unsigned long long want = (unsigned long long) (base + 4096);

    int info[24];
    unsigned long long addr = want + 4096, size = 0x1111;
    unsigned int depth = 77, count = 19;
    for (int i = 0; i < 24; i++) info[i] = GUARD;
    /* does this kernel have PROCMAP_QUERY? asked directly, not through the code under test */
    struct lregion_query probe;
    memset(&probe, 0, sizeof(probe));
    probe.size = sizeof(probe);
    probe.query_flags = 0x10;
    probe.query_addr = want;
    int maps = open("/proc/self/maps", O_RDONLY);
    assert(maps >= 0);
    int have_ioctl = ioctl(maps, LREGION_IOCTL, &probe) == 0;
    close(maps);

    kern_return_t kr = mach_vm_region_recurse(mach_task_self(), &addr, &size, &depth, info, &count);
    if (!have_ioctl) {{
        /* without it every call goes to the server */
        assert(kr == SENTINEL && g_server_calls == 1);
        puts("no-ioctl");
        return 0;
    }}
    assert(kr == KERN_SUCCESS && g_server_calls == 0);
    assert(addr == want && size == 3 * 4096 && depth == 0 && count == 19);
    assert(info[0] == (VM_PROT_READ | VM_PROT_WRITE) && info[19] == GUARD);

    /* an address in the hole below a mapping answers with the next mapping */
    addr = 0x1000; size = 0; depth = 77; count = 12;
    assert(mach_vm_region_recurse(mach_task_self(), &addr, &size, &depth, info, &count) == KERN_SUCCESS);
    assert(addr >= 0x1000 && size != 0 && depth == 0 && count == 12);

    /* past the last mapping: the server's code, and no output is touched */
    addr = 0xfffffffffffff000ull; size = 0x1111; depth = 77; count = 19;
    for (int i = 0; i < 24; i++) info[i] = GUARD;
    assert(mach_vm_region_recurse(mach_task_self(), &addr, &size, &depth, info, &count) == KERN_INVALID_ADDRESS);
    assert(addr == 0xfffffffffffff000ull && size == 0x1111 && depth == 77 && count == 19 && info[0] == GUARD);
    assert(g_server_calls == 0);

    /* a NULL depth is answered here too: the RPC stub would read it */
    addr = want; size = 0; count = 19;
    assert(mach_vm_region_recurse(mach_task_self(), &addr, &size, NULL, info, &count) == KERN_SUCCESS);
    assert(addr == want && size == 3 * 4096 && g_server_calls == 0);

    /* left to the server: another task, another count, a NULL address */
    addr = want; count = 19;
    assert(mach_vm_region_recurse(0x999, &addr, &size, &depth, info, &count) == SENTINEL);
    count = 13;
    assert(mach_vm_region_recurse(mach_task_self(), &addr, &size, &depth, info, &count) == SENTINEL);
    count = 20;
    assert(mach_vm_region_recurse(mach_task_self(), &addr, &size, &depth, info, &count) == SENTINEL);
    count = 19;
    assert(mach_vm_region_recurse(mach_task_self(), NULL, &size, &depth, info, &count) == SENTINEL);
    assert(g_server_calls == 4);

    /* the ioctl fails: the call goes to the server, which still gets a depth to read */
    g_fail_ioctl = 1;
    addr = want; count = 19; g_server_depth = 99;
    assert(mach_vm_region_recurse(mach_task_self(), &addr, &size, NULL, info, &count) == SENTINEL);
    assert(g_server_calls == 5 && g_server_depth == 0);
    depth = 7;
    assert(mach_vm_region_recurse(mach_task_self(), &addr, &size, &depth, info, &count) == SENTINEL);
    assert(g_server_calls == 6 && g_server_depth == 7);
    g_fail_ioctl = 0;
    puts("ok");
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'entry.c'
            binary = Path(folder) / 'entry'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                            '-Wno-unused-variable', '-Wno-unused-parameter',
                            str(source), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn(result.stdout.strip(), ('ok', 'no-ioctl'))


if __name__ == '__main__':
    unittest.main()
