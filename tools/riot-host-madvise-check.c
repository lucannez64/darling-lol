// Host-only check for the darling-lol THP-aware MADV_FREE translation.
//
// The real Darling sys_madvise source is linked against host syscall hooks and
// a read_string() stub that selects the host's transparent-huge-page policy
// ("code path selector"). All memory belongs to this standalone check; no
// client, server or Darling namespace is contacted.
//
// Modes (argv[1]):
//   (none)    the deployment host: THP "always"
//   never     THP disabled on the host
//   madvise   THP only for mappings that ask for it (this layer never does)
#define _GNU_SOURCE
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Links the actual Darling sys_madvise source against host syscall hooks.
long sys_madvise(void* address, unsigned long size, int advice);

#define HUGE_PAGE_SIZE (2ul * 1024 * 1024)

// --- host THP policy as the emulation reads it through read_string() ---
static const char* thpEnabled = "[always] madvise never\n";
static const char* thpPmdSize = "2097152\n";

bool read_string(const char* path, char* dst, int maxlen) {
    const char* value = NULL;
    if (strstr(path, "hpage_pmd_size")) value = thpPmdSize;
    else if (strstr(path, "enabled")) value = thpEnabled;
    if (!value) return false;
    snprintf(dst, maxlen, "%s", value);
    return true;
}

// --- syscall hook ---
static int lastAdvice = -1, conversions = 0;
static unsigned long lastAddress = 0, lastLength = 0, madviseCalls = 0;
long __lolsys_syscall(long a, long b, long c, long d, long e, long f, int nr) {
    if (nr == SYS_madvise) {
        ++madviseCalls;
        lastAdvice = (int)c;
        lastAddress = (unsigned long)a;
        lastLength = (unsigned long)b;
    }
    long result = syscall(nr, a, b, c, d, e, f);
    return result == -1 ? -errno : result;
}

int errno_linux_to_bsd(int error) {
    // EINVAL and ENOMEM have the same numeric values on these two systems.
    ++conversions;
    return error;
}

// The emulation's own parser (src/common/simple.c) links into the library; the
// host check only needs the decimal form it uses for hpage_pmd_size.
unsigned long long __simple_atoi(const char* str, const char** endp) {
    unsigned long long value = 0;
    while (*str >= '0' && *str <= '9') {
        value = value * 10 + (unsigned long long)(*str - '0');
        ++str;
    }
    if (endp) *endp = str;
    return value;
}

static void require(int condition, const char* message) {
    if (!condition) { fprintf(stderr, "HOST_MADVISE pass=0 error=%s\n", message); exit(1); }
}

static unsigned long lazyFreeKB(void) {
    FILE* file = fopen("/proc/self/smaps_rollup", "r");
    require(file != NULL, "smaps_rollup");
    char line[256]; unsigned long value = 0;
    while (fgets(line, sizeof(line), file)) if (sscanf(line, "LazyFree: %lu kB", &value) == 1) break;
    fclose(file); return value;
}

// AnonHugePages of the single VMA that contains the given address.
static unsigned long mappingAnonHugeKB(void* address, int* found) {
    FILE* file = fopen("/proc/self/smaps", "r");
    require(file != NULL, "smaps");
    unsigned long start, end, value = 0, result = 0;
    int inside = 0;
    *found = 0;
    char line[256];
    while (fgets(line, sizeof(line), file)) {
        if (sscanf(line, "%lx-%lx", &start, &end) == 2) {
            inside = (unsigned long)address >= start && (unsigned long)address < end;
            if (inside) { *found = 1; result = 0; }
            continue;
        }
        if (inside && sscanf(line, "AnonHugePages: %lu kB", &value) == 1) result = value;
    }
    fclose(file);
    return result;
}

static char policy[64];

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "always";
    if (strcmp(mode, "never") == 0) thpEnabled = "always madvise [never]\n";
    else if (strcmp(mode, "madvise") == 0) thpEnabled = "always [madvise] never\n";

    // The real host policy decides whether the no-split property can be
    // observed at all.
    FILE* hostPolicy = fopen("/sys/kernel/mm/transparent_hugepage/enabled", "r");
    if (hostPolicy) { fgets(policy, sizeof(policy), hostPolicy); fclose(hostPolicy); }

    // A 2 MiB-aligned 32 MiB region inside a larger mapping. A 32 MiB region is
    // the size at which this host reliably backs the touched pages with 2 MiB
    // huge pages, which is what the sub-huge-page case observes.
    const size_t size = 32 * 1024 * 1024;
    unsigned char* mapping = mmap(NULL, size + HUGE_PAGE_SIZE, PROT_READ | PROT_WRITE,
                                  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    require(mapping != MAP_FAILED, "anonymous mapping");
    unsigned char* memory = (unsigned char*)(((unsigned long)mapping + HUGE_PAGE_SIZE - 1)
                                             & ~(HUGE_PAGE_SIZE - 1));
    // Ask the host for huge pages explicitly so the sub-huge-page case has some
    // to observe; this is the check's own mapping, not the guest path.
    madvise(mapping, size + HUGE_PAGE_SIZE, MADV_HUGEPAGE);
    memset(memory, 0x5a, size);

    int found = 0;
    unsigned long hugeBefore = 0, hugeAfter = 0, subPageCalls = 0;
    unsigned long marked = 0, before = 0, reused = 0;

    if (strcmp(mode, "always") == 0) {
        // A sub-huge-page hint must not reach Linux and must not split the
        // region's huge pages.
        hugeBefore = mappingAnonHugeKB(memory, &found);
        require(found, "own mapping missing from smaps");
        require(!strstr(policy, "[always]") || hugeBefore > 0,
                "host THP always but the region has no huge pages");
        unsigned long callsBefore = madviseCalls;
        require(sys_madvise(memory + 4096, 4096, 7) == 0, "sub-page free hint failed");
        require(sys_madvise(memory, 4096, 5) == 0, "sub-page plain free hint failed");
        subPageCalls = madviseCalls - callsBefore;
        require(subPageCalls == 0, "sub-page hint reached Linux");
        int foundAfter = 0;
        hugeAfter = mappingAnonHugeKB(memory, &foundAfter);
        require(foundAfter && hugeAfter == hugeBefore, "sub-page hint split a huge page");

        // The aligned region is marked reclaimable as whole huge pages and
        // cancelled on reuse with the contents preserved.
        before = lazyFreeKB();
        callsBefore = madviseCalls;
        require(sys_madvise(memory, size, 7) == 0, "Darwin free reusable");
        require(madviseCalls == callsBefore + 1 && lastAdvice == 8, "aligned region translation");
        require(lastAddress == (unsigned long)memory && lastLength == size, "aligned region changed");
        marked = lazyFreeKB();
        require(marked >= before + size / 1024, "aligned pages not marked reclaimable");
        require(sys_madvise(memory, size, 8) == 0, "Darwin reuse");
        require(lastAdvice == 23 && lastAddress == (unsigned long)memory && lastLength == size, "reuse translation");
        reused = lazyFreeKB();
        require(reused <= before, "reuse did not cancel lazy reclaim");
        for (size_t i = 0; i < size; ++i) require(memory[i] == 0x5a, "reuse changed surviving contents");

        // An unaligned large range only affects the whole huge pages inside it.
        memset(memory, 0x37, size);
        callsBefore = madviseCalls;
        require(sys_madvise(memory + HUGE_PAGE_SIZE / 2, size, 7) == 0, "unaligned free");
        require(madviseCalls == callsBefore + 1, "unaligned range not forwarded");
        require(lastAddress == (unsigned long)memory + HUGE_PAGE_SIZE && lastLength == size - HUGE_PAGE_SIZE,
                "unaligned range did not round inward to the contained huge pages");
        require(sys_madvise(memory, size, 8) == 0, "reuse after unaligned hint");

        // A range that contains no whole huge page is a no-op, not an error.
        callsBefore = madviseCalls;
        require(sys_madvise(memory + HUGE_PAGE_SIZE - 4096, 8192, 7) == 0, "hint without contained huge page");
        require(madviseCalls == callsBefore, "hint with no contained huge page reached Linux");
    } else {
        // With THP "never" (or "madvise") the full range is forwarded as before.
        unsigned long callsBefore = madviseCalls;
        require(sys_madvise(memory + 4096, 4096, 7) == 0, "sub-page free hint failed");
        require(madviseCalls == callsBefore + 1 && lastAdvice == 8, "disabled THP did not forward the hint");
        require(lastAddress == (unsigned long)memory + 4096 && lastLength == 4096, "disabled THP changed the range");
        before = lazyFreeKB();
        require(sys_madvise(memory, size, 7) == 0, "Darwin free reusable");
        marked = lazyFreeKB();
        require(marked >= before + size / 1024, "full range not marked reclaimable");
        require(sys_madvise(memory, size, 8) == 0, "Darwin reuse");
        require(lastAdvice == 23 && lastLength == size, "reuse translation");
        reused = lazyFreeKB();
        require(reused <= before, "reuse did not cancel lazy reclaim");
        for (size_t i = 0; i < size; ++i) require(memory[i] == 0x5a, "reuse changed surviving contents");
    }

    // Other advice values keep the existing translation and error path.
    unsigned long callsBefore = madviseCalls;
    require(sys_madvise(memory, size, 0) == 0 && lastAdvice == 0 && madviseCalls == callsBefore + 1,
            "normal hint passthrough");
    // An unaligned REUSE is refused before Linux sees it, with Darwin's EINVAL.
    callsBefore = madviseCalls;
    require(sys_madvise(memory + 1, size - 1, 8) == -EINVAL, "unaligned reuse error");
    require(madviseCalls == callsBefore && conversions == 0, "unaligned reuse reached Linux");

    require(munmap(mapping, size + HUGE_PAGE_SIZE) == 0, "unmap");
    printf("HOST_MADVISE pass=1 mode=%s madvise_calls=%lu sub_page_syscalls=%lu reclaimable_KiB=%lu "
           "reuse_reclaimable_KiB=%lu anon_huge_KiB=%lu/%lu contents=preserved\n",
           mode, madviseCalls, subPageCalls, marked - before, reused - before, hugeBefore, hugeAfter);
    return 0;
}
