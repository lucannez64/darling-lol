"""host_statistics64(HOST_VM_INFO64) reports host memory and stays within hw.memsize."""
from pathlib import Path
import shutil
import unittest

import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]

MACH_TYPES = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
typedef int kern_return_t;
typedef unsigned int natural_t;
typedef int integer_t;
typedef unsigned int mach_msg_type_number_t;
typedef unsigned int host_t;
typedef int host_flavor_t;
typedef integer_t *host_info64_t;
#define KERN_SUCCESS 0
#define KERN_FAILURE 5
#define PAGE_SIZE 4096
#define HOST_VM_INFO64 4
struct vm_statistics64 {
    natural_t free_count, active_count, inactive_count, wire_count;
    uint64_t zero_fill_count, reactivations, pageins, pageouts, faults, cow_faults, lookups, hits, purges;
    natural_t purgeable_count, speculative_count;
    uint64_t decompressions, compressions, swapins, swapouts;
    natural_t compressor_page_count, throttled_count, external_page_count, internal_page_count;
    uint64_t total_uncompressed_pages_in_compressor;
} __attribute__((aligned(8)));
typedef struct vm_statistics64 *vm_statistics64_t;
typedef struct vm_statistics64 vm_statistics64_data_t;
#define HOST_VM_INFO64_COUNT ((mach_msg_type_number_t)(sizeof(vm_statistics64_data_t) / sizeof(integer_t)))
#define HOST_VM_INFO64_REV1_COUNT HOST_VM_INFO64_COUNT
#define HOST_VM_INFO64_REV0_COUNT (HOST_VM_INFO64_REV1_COUNT - 14)
'''


def added_lines(patch_name, path):
    patch = (ROOT / 'patches' / patch_name).read_text()
    section = patch.split(f'diff --git a/{path} ', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


@unittest.skipUnless(shutil.which('cc') and shutil.which('patch'), 'C compiler and patch required')
class HostVmStatisticsTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c
    patched = sources.CompatibilityChecks.patched

    def test_server_maps_proc_meminfo_onto_vm_statistics64(self):
        source = self.patched('darlingserver', 'duct-tape/src/host.c')
        code = 'struct dtape_meminfo {' + source.split('struct dtape_meminfo {', 1)[1].split('};', 1)[0] + '};\n'
        code += ''.join(sources.function(source, name)
                        for name in ('dtape_meminfo_field', 'dtape_read_meminfo', 'vm_stats'))
        self.run_c(MACH_TYPES + r'''
static const char *fixture;
static long offset;
static int open(const char *path, int flags) {
    assert(strcmp(path, "/proc/meminfo") == 0 && flags == 0);
    offset = 0;
    return fixture ? 7 : -1;
}
static long read(int fd, void *buffer, unsigned long count) {
    unsigned long left = strlen(fixture) - offset;
    assert(fd == 7);
    if (count > 5) count = 5; /* short reads */
    if (count > left) count = left;
    memcpy(buffer, fixture + offset, count);
    offset += count;
    return (long)count;
}
static int close(int fd) { assert(fd == 7); return 0; }
''' + code + r'''
int main(void) {
    integer_t raw[104];
    vm_statistics64_t stat = (vm_statistics64_t)raw;
    mach_msg_type_number_t count = 104; /* the count the game passes */

    fixture =
        "MemTotal:       16000000 kB\n"
        "MemFree:         1000000 kB\n"
        "MemAvailable:    6000000 kB\n"
        "Buffers:          100000 kB\n"
        "Cached:          5000000 kB\n"
        "Active:          4000000 kB\n"
        "Inactive:        6000000 kB\n"
        "Active(anon):    3000000 kB\n"
        "Inactive(anon):  2000000 kB\n"
        "Active(file):    1000000 kB\n"
        "Inactive(file):  4000000 kB\n"
        "Unevictable:       40000 kB\n";
    memset(raw, 0xff, sizeof(raw));
    assert(vm_stats(raw, &count) == KERN_SUCCESS);
    assert(count == HOST_VM_INFO64_COUNT);
    assert(stat->free_count == 250000 && stat->active_count == 1000000);
    assert(stat->inactive_count == 1500000 && stat->wire_count == 1250000);
    assert(stat->external_page_count == 1250000 && stat->internal_page_count == 1250000);
    /* the four page classes add up to MemTotal, the size host_info reports */
    assert((uint64_t)stat->free_count + stat->active_count + stat->inactive_count
        + stat->wire_count == 16000000ull * 1024 / PAGE_SIZE);
    assert(stat->faults == 0 && stat->compressor_page_count == 0 && stat->speculative_count == 0);
    assert(raw[HOST_VM_INFO64_COUNT] == -1); /* nothing written past the structure */

    /* rev0 callers get the shorter structure and nothing beyond it */
    count = HOST_VM_INFO64_REV0_COUNT;
    memset(raw, 0xff, sizeof(raw));
    assert(vm_stats(raw, &count) == KERN_SUCCESS && count == HOST_VM_INFO64_REV0_COUNT);
    assert(stat->free_count == 250000 && raw[HOST_VM_INFO64_REV0_COUNT] == -1);
    count = HOST_VM_INFO64_REV0_COUNT - 1;
    assert(vm_stats(raw, &count) == KERN_FAILURE);

    /* unreadable or unusable /proc/meminfo: success with zeroed statistics */
    fixture = NULL;
    count = HOST_VM_INFO64_COUNT;
    memset(raw, 0xff, sizeof(raw));
    assert(vm_stats(raw, &count) == KERN_SUCCESS && stat->free_count == 0 && stat->wire_count == 0);
    fixture = "MemFree: 5 kB\n";
    memset(raw, 0xff, sizeof(raw));
    assert(vm_stats(raw, &count) == KERN_SUCCESS && stat->free_count == 0);
    return 0;
}
''')

    def test_guest_wrapper_keeps_page_counts_within_hw_memsize(self):
        added = added_lines('xnu.patch', 'libsyscall/mach/host.c')
        wrapper = 'kern_return_t\nhost_statistics64(' + added.split('kern_return_t\nhost_statistics64(', 1)[1]
        self.run_c(MACH_TYPES + r'''
static unsigned long long reported_bytes, host_bytes;
static kern_return_t server_result;
static void darling_hw_memsize(unsigned long long *reported, unsigned long long *host_real) {
    *reported = reported_bytes; *host_real = host_bytes;
}
static kern_return_t _kernelrpc_host_statistics64(host_t host, host_flavor_t flavor,
    host_info64_t info, mach_msg_type_number_t *count) {
    vm_statistics64_t stat = (vm_statistics64_t)info;
    (void)host; (void)flavor;
    memset(stat, 0, sizeof(*stat));
    stat->free_count = 2000000; stat->active_count = 3000000;
    stat->inactive_count = 2000000; stat->wire_count = 1000000; /* 8M pages = 32000 MiB */
    stat->external_page_count = 2500000; stat->internal_page_count = 2500000;
    stat->faults = 777;
    *count = HOST_VM_INFO64_COUNT;
    return server_result;
}
''' + wrapper + r'''
static uint64_t total(const struct vm_statistics64 *stat) {
    return (uint64_t)stat->free_count + stat->active_count + stat->inactive_count + stat->wire_count;
}
int main(void) {
    struct vm_statistics64 stat;
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    host_bytes = 8000000ull * PAGE_SIZE;

    /* profile memory equal to or above the host's: host numbers pass through */
    reported_bytes = host_bytes;
    assert(host_statistics64(1, HOST_VM_INFO64, (host_info64_t)&stat, &count) == KERN_SUCCESS);
    assert(stat.free_count == 2000000 && total(&stat) == 8000000);
    reported_bytes = 2 * host_bytes;
    assert(host_statistics64(1, HOST_VM_INFO64, (host_info64_t)&stat, &count) == KERN_SUCCESS);
    assert(stat.free_count == 2000000 && stat.external_page_count == 2500000);

    /* profile memory below the host's: scaled, never above hw.memsize */
    reported_bytes = 17179869184ull;
    assert(host_statistics64(1, HOST_VM_INFO64, (host_info64_t)&stat, &count) == KERN_SUCCESS);
    assert(total(&stat) * PAGE_SIZE <= reported_bytes);
    assert(total(&stat) * PAGE_SIZE > reported_bytes - (64ull << 20));
    assert(stat.free_count > 1040000 && stat.free_count <= 1048576);
    assert(stat.external_page_count + stat.internal_page_count <= stat.active_count + stat.inactive_count);
    assert(stat.faults == 777); /* event counters are not page counts */

    /* other flavors, failures and an unknown memory size are left alone */
    assert(host_statistics64(1, 5, (host_info64_t)&stat, &count) == KERN_SUCCESS);
    assert(stat.free_count == 2000000);
    server_result = KERN_FAILURE;
    assert(host_statistics64(1, HOST_VM_INFO64, (host_info64_t)&stat, &count) == KERN_FAILURE);
    assert(stat.free_count == 2000000);
    server_result = KERN_SUCCESS;
    reported_bytes = 0;
    assert(host_statistics64(1, HOST_VM_INFO64, (host_info64_t)&stat, &count) == KERN_SUCCESS);
    assert(stat.free_count == 2000000);
    return 0;
}
''')

    def test_hw_memsize_and_the_wrapper_share_one_source(self):
        xnu = (ROOT / 'patches/xnu.patch').read_text()
        self.assertIn('_kernelrpc_host_statistics64(', added_lines('xnu.patch', 'osfmk/mach/mach_host.defs'))
        self.assertIn('host_real_mem = hinfo.max_mem;', xnu)
        self.assertIn('*reported = gethostinfo()->max_mem;', xnu)


if __name__ == '__main__':
    unittest.main()
