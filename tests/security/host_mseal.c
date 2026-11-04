#define _GNU_SOURCE
#include "dc_mseal.h"
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>
#include <assert.h>
#include <fcntl.h>
#ifndef __NR_mseal
#define __NR_mseal 462
#endif
static long allocate(unsigned long len) {
    void* p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    return p == MAP_FAILED ? -errno : (long)p;
}
static long protect(void* p, unsigned long len, int prot) { return mprotect(p, len, prot) ? -errno : 0; }
static long relocate(void* p, unsigned long len, void* target) {
    void* result = mremap(p, len, len, MREMAP_MAYMOVE | MREMAP_FIXED, target);
    return result == MAP_FAILED ? -errno : (long)result;
}
static void release(void* p, unsigned long len) { assert(!munmap(p, len)); }
static long seal(void* p, unsigned long len) { return syscall(__NR_mseal, p, len, 0) ? -errno : 0; }
static long refused(unsigned long len) { (void)len; return -ENOMEM; }
int main(void) {
    char name[] = "/tmp/dc-seal-test-XXXXXX";
    int fd = mkstemp(name); assert(fd >= 0); assert(!unlink(name));
    char bytes[4096]; memset(bytes, 'A', sizeof(bytes)); assert(write(fd, bytes, sizeof(bytes)) == sizeof(bytes));
    char* p = mmap(NULL, sizeof(bytes), PROT_READ, MAP_PRIVATE, fd, 0); assert(p != MAP_FAILED);
    struct dc_seal_ops ops = { allocate, protect, relocate, release, seal };
    assert(dc_snapshot_and_seal(p, sizeof(bytes), PROT_READ | PROT_WRITE, &ops) == -EINVAL);
    ops.allocate = refused;
    assert(dc_snapshot_and_seal(p, sizeof(bytes), PROT_READ, &ops) == -ENOMEM); assert(p[0] == 'A');
    ops.allocate = allocate;
    long result = dc_snapshot_and_seal(p, sizeof(bytes), PROT_READ, &ops);
    if (result == -ENOSYS) { puts("SKIP mseal kernel enforcement unavailable"); return 77; }
    assert(result == 0);
    assert(pwrite(fd, "B", 1, 0) == 1); assert(p[0] == 'A');
    assert(mprotect(p, sizeof(bytes), PROT_READ | PROT_WRITE) == -1 && errno == EPERM);
    assert(munmap(p, sizeof(bytes)) == -1 && errno == EPERM);
    assert(mmap(p, sizeof(bytes), PROT_READ, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0) == MAP_FAILED && errno == EPERM);
    assert(madvise(p, sizeof(bytes), MADV_DONTNEED) == -1 && errno == EPERM);
    assert(!ftruncate(fd, 0)); assert(p[0] == 'A');
    close(fd);
    puts("PASS sealed anonymous snapshot survives backing writes/truncation and rejects remap, discard and protection changes");
}
