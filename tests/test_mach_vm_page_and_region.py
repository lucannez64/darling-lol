"""Verify darlingserver mach_vm_page_info/query/range_query and mach_vm_region_recurse.

Compiles the patched mach_vm_page_* functions directly from patches/darlingserver.patch
so they always run on CI without a prepared Darling checkout, and compiles the full
patched mach_vm_region_recurse() from memory.c when prepared sources are present.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darlingserver.patch'


def patch_section(target: str) -> str:
    text = PATCH.read_text()
    marker = f'diff --git a/{target} b/{target}\n'
    return text.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def reconstructed_new_text(target: str) -> str:
    """Return all context (' ') and added ('+') lines from a patch section."""
    out = []
    in_hunk = False
    for line in patch_section(target).splitlines():
        if line.startswith('@@ '):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith('+') or line.startswith(' '):
            out.append(line[1:])
    return '\n'.join(out) + '\n'


def full_patched_file(target: str):
    """Apply the target file's patch section to the pinned upstream revision if available."""
    manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
    comp = next(c for c in manifest['components'] if c['patch'] == 'patches/darlingserver.patch')
    repo = ROOT / 'build/darling-src' / comp['path']
    if not (repo / '.git').exists() or not shutil.which('patch'):
        return None
    original = subprocess.check_output(['git', '-C', str(repo), 'show', comp['revision'] + ':' + target])
    marker = f'diff --git a/{target} b/{target}\n'
    section = patch_section(target)
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / target
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(original)
        subprocess.run(
            ['patch', '--silent', '-p1', '-d', td],
            input=marker + section + ('' if section.endswith('\n') else '\n'),
            text=True,
            check=True,
            capture_output=True,
        )
        return out_path.read_text()


def extract_function(source: str, name: str) -> str:
    m = re.search(
        r'^(?:static\s+)?[\w* ]+\b' + re.escape(name) + r'\([^;]*?\)\s*\{',
        source,
        re.M | re.S,
    )
    if not m:
        raise AssertionError(f'Function {name} not found in patch section')
    start = m.start()
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


HARNESS_PREFIX = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef int kern_return_t;
typedef int vm_prot_t;
typedef unsigned int vm_inherit_t;
typedef int vm_behavior_t;
typedef int boolean_t;
typedef unsigned int natural_t;
typedef unsigned int mach_msg_type_number_t;
typedef uint64_t mach_vm_address_t;
typedef uint64_t mach_vm_offset_t;
typedef uint64_t mach_vm_size_t;
typedef uint64_t memory_object_offset_t;
typedef uint64_t vm_object_id_t;
typedef uint32_t vm32_object_id_t;
typedef size_t vm_size_t;
typedef int vm_page_info_flavor_t;
typedef int* vm_page_info_t;
typedef int* vm_region_recurse_info_t;

#define TRUE 1
#define FALSE 0

#define KERN_SUCCESS 0
#define KERN_INVALID_ADDRESS 1
#define KERN_INVALID_ARGUMENT 4
#define KERN_FAILURE 5

#define VM_PROT_NONE    0x0
#define VM_PROT_READ    0x1
#define VM_PROT_WRITE   0x2
#define VM_PROT_EXECUTE 0x4

#define VM_INHERIT_SHARE 0
#define VM_INHERIT_COPY  1
#define VM_BEHAVIOR_DEFAULT 0

#define SM_PRIVATE 4
#define SM_SHARED  6

#define VM_PAGE_QUERY_PAGE_PRESENT      0x1
#define VM_PAGE_QUERY_PAGE_REF          0x4
#define VM_PAGE_QUERY_PAGE_DIRTY        0x8
#define VM_PAGE_QUERY_PAGE_PAGED_OUT    0x10
#define VM_PAGE_QUERY_PAGE_EXTERNAL     0x80
#define VM_PAGE_QUERY_PAGE_CS_VALIDATED 0x100

#define VM_PAGE_INFO_BASIC 1

#pragma pack(push, 4)
struct vm_region_submap_info_64 {
    vm_prot_t               protection;
    vm_prot_t               max_protection;
    vm_inherit_t            inheritance;
    memory_object_offset_t  offset;
    unsigned int            user_tag;
    unsigned int            pages_resident;
    unsigned int            pages_shared_now_private;
    unsigned int            pages_swapped_out;
    unsigned int            pages_dirtied;
    unsigned int            ref_count;
    unsigned short          shadow_depth;
    unsigned char           external_pager;
    unsigned char           share_mode;
    boolean_t               is_submap;
    vm_behavior_t           behavior;
    vm32_object_id_t        object_id;
    unsigned short          user_wired_count;
    unsigned int            pages_reusable;
    vm_object_id_t          object_id_full;
};
typedef struct vm_region_submap_info_64 vm_region_submap_info_data_64_t;
typedef struct vm_region_submap_info_64* vm_region_submap_info_64_t;

#define VM_REGION_SUBMAP_INFO_V2_SIZE (sizeof(vm_region_submap_info_data_64_t))
#define VM_REGION_SUBMAP_INFO_V1_SIZE (VM_REGION_SUBMAP_INFO_V2_SIZE - sizeof(vm_object_id_t))
#define VM_REGION_SUBMAP_INFO_V0_SIZE (VM_REGION_SUBMAP_INFO_V1_SIZE - sizeof(unsigned int))
#define VM_REGION_SUBMAP_INFO_V2_COUNT_64 ((mach_msg_type_number_t)(VM_REGION_SUBMAP_INFO_V2_SIZE / sizeof(natural_t)))
#define VM_REGION_SUBMAP_INFO_V1_COUNT_64 ((mach_msg_type_number_t)(VM_REGION_SUBMAP_INFO_V1_SIZE / sizeof(natural_t)))
#define VM_REGION_SUBMAP_INFO_V0_COUNT_64 ((mach_msg_type_number_t)(VM_REGION_SUBMAP_INFO_V0_SIZE / sizeof(natural_t)))
#define VM_REGION_SUBMAP_INFO_COUNT_64 VM_REGION_SUBMAP_INFO_V2_COUNT_64

struct vm_region_submap_short_info_64 {
    vm_prot_t               protection;
    vm_prot_t               max_protection;
    vm_inherit_t            inheritance;
    memory_object_offset_t  offset;
    unsigned int            user_tag;
    unsigned int            ref_count;
    unsigned short          shadow_depth;
    unsigned char           external_pager;
    unsigned char           share_mode;
    boolean_t               is_submap;
    vm_behavior_t           behavior;
    vm32_object_id_t        object_id;
    unsigned short          user_wired_count;
};
typedef struct vm_region_submap_short_info_64 vm_region_submap_short_info_data_64_t;
typedef struct vm_region_submap_short_info_64* vm_region_submap_short_info_64_t;
#define VM_REGION_SUBMAP_SHORT_INFO_COUNT_64 ((mach_msg_type_number_t)(sizeof(vm_region_submap_short_info_data_64_t) / sizeof(natural_t)))
#pragma pack(pop)

struct vm_page_info_basic {
    int                     disposition;
    int                     ref_count;
    vm_object_id_t          object_id;
    memory_object_offset_t  offset;
    int                     depth;
    int                     __pad;
};
typedef struct vm_page_info_basic vm_page_info_basic_data_t;
#define VM_PAGE_INFO_BASIC_COUNT ((mach_msg_type_number_t)(sizeof(vm_page_info_basic_data_t) / sizeof(int)))

typedef enum dtape_memory_protection {
    dtape_memory_protection_none = 0,
    dtape_memory_protection_read = 1 << 0,
    dtape_memory_protection_write = 1 << 1,
    dtape_memory_protection_execute = 1 << 2,
} dtape_memory_protection_t;

typedef struct dtape_memory_region_info {
    uintptr_t start_address;
    uint64_t page_count;
    uint64_t map_offset;
    dtape_memory_protection_t protection;
    bool shared;
} dtape_memory_region_info_t;

typedef struct dtape_page_info {
    uint64_t pagemap_entry;
    bool pagemap_valid;
    bool cs_valid;
} dtape_page_info_t;

typedef struct dtape_task {
    void* context;
} dtape_task_t;

typedef struct vm_map {
    dtape_task_t* dtape_task;
} *vm_map_t;
#define VM_MAP_NULL ((vm_map_t)0)

#define _SC_PAGESIZE 30
static long sysconf(int name) {
    (void)name;
    return 4096;
}
#define dtape_stub_safe(...) ((void)0)

typedef struct fake_region {
    uintptr_t start;
    uint64_t pages;
    uint64_t offset;
    dtape_memory_protection_t prot;
    bool shared;
    uint64_t pagemap_entry;
    bool pagemap_valid;
} fake_region_t;

static fake_region_t g_regions[] = {
    /* Signed r-x code segment (file-backed, present) */
    { 0x100000000ULL, 4, 0x0,
      (dtape_memory_protection_t)(dtape_memory_protection_read | dtape_memory_protection_execute),
      false, (1ULL << 63) | (1ULL << 61), true },
    /* Writable anonymous heap (present, dirty) */
    { 0x100010000ULL, 2, 0x0,
      (dtape_memory_protection_t)(dtape_memory_protection_read | dtape_memory_protection_write),
      false, (1ULL << 63), true },
    /* Swapped page region */
    { 0x100020000ULL, 1, 0x2000,
      (dtape_memory_protection_t)(dtape_memory_protection_read | dtape_memory_protection_write),
      true, (1ULL << 62), true },
    /* Fallback region where pagemap is unreadable */
    { 0x100030000ULL, 1, 0x0,
      (dtape_memory_protection_t)(dtape_memory_protection_read | dtape_memory_protection_execute),
      false, 0, false },
};

static bool g_cs_valid = true;

static bool fake_get_region(void* ctx, uintptr_t addr, dtape_memory_region_info_t* out) {
    (void)ctx;
    for (size_t i = 0; i < sizeof(g_regions) / sizeof(g_regions[0]); i++) {
        uintptr_t end = g_regions[i].start + g_regions[i].pages * 4096ULL;
        if (addr >= g_regions[i].start && addr < end) {
            out->start_address = g_regions[i].start;
            out->page_count = g_regions[i].pages;
            out->map_offset = g_regions[i].offset;
            out->protection = g_regions[i].prot;
            out->shared = g_regions[i].shared;
            return true;
        }
    }
    return false;
}

static uintptr_t fake_get_next_region(void* ctx, uintptr_t addr) {
    (void)ctx;
    for (size_t i = 0; i < sizeof(g_regions) / sizeof(g_regions[0]); i++) {
        if (g_regions[i].start > addr) {
            return g_regions[i].start;
        }
    }
    return 0;
}

static bool fake_get_page_info(void* ctx, uintptr_t page_addr, dtape_page_info_t* out) {
    (void)ctx;
    out->cs_valid = g_cs_valid;
    for (size_t i = 0; i < sizeof(g_regions) / sizeof(g_regions[0]); i++) {
        uintptr_t end = g_regions[i].start + g_regions[i].pages * 4096ULL;
        if (page_addr >= g_regions[i].start && page_addr < end) {
            out->pagemap_entry = g_regions[i].pagemap_entry;
            out->pagemap_valid = g_regions[i].pagemap_valid;
            return true;
        }
    }
    out->pagemap_entry = 0;
    out->pagemap_valid = false;
    return true;
}

struct dtape_hooks_table {
    bool (*task_get_memory_region_info)(void*, uintptr_t, dtape_memory_region_info_t*);
    uintptr_t (*task_get_next_region)(void*, uintptr_t);
    bool (*task_get_page_info)(void*, uintptr_t, dtape_page_info_t*);
};
static struct dtape_hooks_table g_hooks = {
    fake_get_region,
    fake_get_next_region,
    fake_get_page_info,
};
static struct dtape_hooks_table* dtape_hooks = &g_hooks;

static int g_copyout_buf[64];
static bool g_fail_copyout = false;
static int copyoutmap(vm_map_t map, const void* from, mach_vm_address_t to, vm_size_t len) {
    (void)map;
    if (g_fail_copyout) {
        return 1;
    }
    assert(to >= 0x200000ULL && to + len <= 0x200000ULL + sizeof(g_copyout_buf));
    memcpy((char*)g_copyout_buf + (to - 0x200000ULL), from, len);
    return 0;
}
'''

HARNESS_MAIN = r'''
int main(void) {
    dtape_task_t task = { .context = (void*)1 };
    struct vm_map map_obj = { .dtape_task = &task };
    vm_map_t map = &map_obj;

    /* --- 1. mach_vm_page_info argument validation --- */
    mach_msg_type_number_t cnt = 123;
    vm_page_info_basic_data_t basic;
    assert(mach_vm_page_info(VM_MAP_NULL, 0, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, &cnt) == KERN_INVALID_ARGUMENT && cnt == 0);
    assert(mach_vm_page_info(map, 0, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, NULL) == KERN_INVALID_ARGUMENT);
    cnt = VM_PAGE_INFO_BASIC_COUNT;
    assert(mach_vm_page_info(map, 0, 99, (vm_page_info_t)&basic, &cnt) == KERN_INVALID_ARGUMENT && cnt == 0);
    cnt = 2;
    assert(mach_vm_page_info(map, 0, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, &cnt) == KERN_INVALID_ARGUMENT && cnt == 0);

    /* --- 2. mach_vm_page_info on signed r-x code page --- */
    cnt = VM_PAGE_INFO_BASIC_COUNT;
    assert(mach_vm_page_info(map, 0x100002123ULL, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, &cnt) == KERN_SUCCESS);
    assert(cnt == VM_PAGE_INFO_BASIC_COUNT);
    assert(basic.disposition == (VM_PAGE_QUERY_PAGE_PRESENT | VM_PAGE_QUERY_PAGE_REF | VM_PAGE_QUERY_PAGE_EXTERNAL | VM_PAGE_QUERY_PAGE_CS_VALIDATED));
    assert(basic.ref_count == 1);
    assert(basic.object_id == (0x100000000ULL >> 12));
    assert(basic.offset == 0x2000ULL);
    assert(basic.depth == 0);

    /* Backwards-compatible count (VM_PAGE_INFO_BASIC_COUNT - 1) must not touch __pad */
    basic.__pad = 0x5a5a5a5a;
    cnt = VM_PAGE_INFO_BASIC_COUNT - 1;
    assert(mach_vm_page_info(map, 0x100010000ULL, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, &cnt) == KERN_SUCCESS);
    assert(basic.__pad == 0x5a5a5a5a);
    assert(basic.disposition == (VM_PAGE_QUERY_PAGE_PRESENT | VM_PAGE_QUERY_PAGE_REF | VM_PAGE_QUERY_PAGE_DIRTY));
    assert(basic.ref_count == 1);

    /* Swapped page */
    cnt = VM_PAGE_INFO_BASIC_COUNT;
    assert(mach_vm_page_info(map, 0x100020000ULL, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, &cnt) == KERN_SUCCESS);
    assert(basic.disposition == VM_PAGE_QUERY_PAGE_PAGED_OUT);
    assert(basic.ref_count == 1);
    assert(basic.offset == 0x2000ULL);

    /* Unreadable pagemap fallback */
    cnt = VM_PAGE_INFO_BASIC_COUNT;
    assert(mach_vm_page_info(map, 0x100030000ULL, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, &cnt) == KERN_SUCCESS);
    assert(basic.disposition == (VM_PAGE_QUERY_PAGE_PRESENT | VM_PAGE_QUERY_PAGE_REF | VM_PAGE_QUERY_PAGE_CS_VALIDATED));

    /* Unmapped address returns KERN_SUCCESS with zeroed info */
    cnt = VM_PAGE_INFO_BASIC_COUNT;
    assert(mach_vm_page_info(map, 0x100008000ULL, VM_PAGE_INFO_BASIC, (vm_page_info_t)&basic, &cnt) == KERN_SUCCESS);
    assert(basic.disposition == 0 && basic.ref_count == 0 && basic.object_id == 0 && basic.offset == 0);

    /* --- 3. mach_vm_page_query --- */
    int disp = 99, refs = 99;
    assert(mach_vm_page_query(VM_MAP_NULL, 0, &disp, &refs) == KERN_INVALID_ARGUMENT && disp == 0 && refs == 0);
    assert(mach_vm_page_query(map, 0x100010000ULL, &disp, &refs) == KERN_SUCCESS);
    assert(disp == (VM_PAGE_QUERY_PAGE_PRESENT | VM_PAGE_QUERY_PAGE_REF | VM_PAGE_QUERY_PAGE_DIRTY) && refs == 1);

    /* --- 4. mach_vm_page_range_query --- */
    mach_vm_size_t rcnt = 6;
    assert(mach_vm_page_range_query(map, 0x100002000ULL, 6 * 4096ULL, 0x200000ULL, &rcnt) == KERN_SUCCESS);
    assert(rcnt == 6);
    /* Pages 0x100002000, 0x100003000 are mapped r-x; 0x100004000..0x100007000 are unmapped */
    assert((g_copyout_buf[0] & VM_PAGE_QUERY_PAGE_CS_VALIDATED) != 0);
    assert((g_copyout_buf[1] & VM_PAGE_QUERY_PAGE_CS_VALIDATED) != 0);
    assert(g_copyout_buf[2] == 0 && g_copyout_buf[3] == 0);

    rcnt = 4;
    assert(mach_vm_page_range_query(map, UINT64_MAX - 10, 4096, 0x200000ULL, &rcnt) == KERN_INVALID_ARGUMENT && rcnt == 0);
    g_fail_copyout = true;
    rcnt = 2;
    assert(mach_vm_page_range_query(map, 0x100000000ULL, 8192, 0x200000ULL, &rcnt) == KERN_INVALID_ADDRESS);
    g_fail_copyout = false;

    /* --- 5. Optional mach_vm_region_recurse checks when full source is compiled --- */
#ifdef TEST_REGION_RECURSE
    struct {
        vm_region_submap_short_info_data_64_t info;
        uint32_t canary[8];
    } short_box;
    memset(&short_box, 0xaa, sizeof(short_box));
    mach_vm_address_t addr = 0x100020000ULL;
    mach_vm_size_t sz = 0;
    uint32_t depth = 7;
    cnt = VM_REGION_SUBMAP_SHORT_INFO_COUNT_64;
    assert(mach_vm_region_recurse(map, &addr, &sz, &depth, (vm_region_recurse_info_t)&short_box.info, &cnt) == KERN_SUCCESS);
    assert(cnt == VM_REGION_SUBMAP_SHORT_INFO_COUNT_64);
    assert(addr == 0x100020000ULL && sz == 4096ULL && depth == 0);
    assert(short_box.info.protection == (VM_PROT_READ | VM_PROT_WRITE));
    assert(short_box.info.max_protection == (VM_PROT_READ | VM_PROT_WRITE));
    assert(short_box.info.share_mode == SM_SHARED);
    assert(short_box.info.inheritance == VM_INHERIT_SHARE);
    assert(short_box.info.ref_count == 1);
    for (int i = 0; i < 8; i++) {
        assert(short_box.canary[i] == 0xaaaaaaaaU);
    }

    /* --- 6. mach_vm_region_recurse: FULL V2 / V1 / V0 info --- */
    vm_region_submap_info_data_64_t full;
    memset(&full, 0xee, sizeof(full));
    addr = 0x100000100ULL;
    cnt = VM_REGION_SUBMAP_INFO_COUNT_64;
    assert(mach_vm_region_recurse(map, &addr, &sz, &depth, (vm_region_recurse_info_t)&full, &cnt) == KERN_SUCCESS);
    assert(cnt == VM_REGION_SUBMAP_INFO_COUNT_64);
    assert(addr == 0x100000000ULL && sz == 4 * 4096ULL);
    assert(full.protection == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(full.max_protection == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(full.share_mode == SM_PRIVATE);
    assert(full.pages_resident == 4);
    assert(full.ref_count == 1);
    assert(full.object_id_full == (0x100000000ULL >> 12));

    /* V0 count must not touch pages_reusable or object_id_full */
    memset(&full, 0xcc, sizeof(full));
    addr = 0x100010000ULL;
    cnt = VM_REGION_SUBMAP_INFO_V0_COUNT_64;
    assert(mach_vm_region_recurse(map, &addr, &sz, &depth, (vm_region_recurse_info_t)&full, &cnt) == KERN_SUCCESS);
    assert(cnt == VM_REGION_SUBMAP_INFO_V0_COUNT_64);
    assert(full.pages_reusable == 0xccccccccU);
    assert(full.object_id_full == 0xccccccccccccccccULL);

    /* Invalid count must return KERN_INVALID_ARGUMENT (not overwritten with KERN_SUCCESS) */
    addr = 0x100000000ULL;
    cnt = 5;
    assert(mach_vm_region_recurse(map, &addr, &sz, &depth, (vm_region_recurse_info_t)&full, &cnt) == KERN_INVALID_ARGUMENT);

    /* Past end of address space returns KERN_INVALID_ADDRESS */
    addr = 0x900000000ULL;
    cnt = VM_REGION_SUBMAP_INFO_COUNT_64;
    assert(mach_vm_region_recurse(map, &addr, &sz, &depth, (vm_region_recurse_info_t)&full, &cnt) == KERN_INVALID_ADDRESS);
#else
    /* Verify helper protection mapping directly even without prepared upstream source */
    assert(dtape_prot_from_region((dtape_memory_protection_t)(dtape_memory_protection_read | dtape_memory_protection_execute)) == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(dtape_max_prot_from_prot(VM_PROT_READ | VM_PROT_EXECUTE) == (VM_PROT_READ | VM_PROT_EXECUTE));
    assert(dtape_max_prot_from_prot(VM_PROT_READ | VM_PROT_WRITE) == (VM_PROT_READ | VM_PROT_WRITE));
    assert(dtape_max_prot_from_prot(VM_PROT_NONE) == VM_PROT_NONE);
#endif

    return 0;
}
'''


class MachVmPageAndRegionTests(unittest.TestCase):
    def test_patch_wires_page_info_hook_and_region_recurse_fixes(self):
        self.assertIn('+typedef struct dtape_page_info {', patch_section('duct-tape/include/darlingserver/duct-tape/types.h'))
        self.assertIn('task_get_page_info', patch_section('duct-tape/include/darlingserver/duct-tape/hooks.h'))
        self.assertIn('queryPageInfo', patch_section('internal-include/darlingserver/process.hpp'))
        self.assertIn('/pagemap', patch_section('src/process.cpp'))
        self.assertIn('dtape_hook_task_get_page_info', patch_section('src/server.cpp'))
        mem_sec = patch_section('duct-tape/src/memory.c')
        self.assertIn('+		vm_region_submap_short_info_data_64_t out;', mem_sec)
        self.assertIn('+	    requested_cnt != VM_REGION_SUBMAP_INFO_V2_COUNT_64)', mem_sec)
        self.assertIn('+		return KERN_INVALID_ARGUMENT;', mem_sec)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_compiled_page_info_and_region_recurse(self):
        full_src = full_patched_file('duct-tape/src/memory.c')
        src = full_src if full_src is not None else reconstructed_new_text('duct-tape/src/memory.c')
        funcs = [
            'dtape_prot_from_region',
            'dtape_max_prot_from_prot',
            'dtape_query_page_basic',
            'mach_vm_page_info',
            'mach_vm_page_query',
            'mach_vm_page_range_query',
        ]
        cflags = ['-std=c11', '-Wall', '-Wextra', '-Werror']
        if full_src is not None:
            funcs.append('mach_vm_region_recurse')
            cflags.append('-DTEST_REGION_RECURSE')
        body = ''.join(extract_function(src, f) for f in funcs)
        code = HARNESS_PREFIX + body + HARNESS_MAIN
        with tempfile.TemporaryDirectory() as td:
            cfile = Path(td) / 'test_vm.c'
            binary = Path(td) / 'test_vm'
            cfile.write_text(code)
            comp = subprocess.run(
                ['cc', *cflags, str(cfile), '-o', str(binary)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(comp.returncode, 0, comp.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
