#define _GNU_SOURCE
#include "dc_image_snapshot.h"
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
static int interrupted;
static long size_of(int fd) { struct stat st; return fstat(fd, &st) == 0 ? st.st_size : -errno; }
static long create_image(void) { long r = syscall(__NR_memfd_create, "snapshot-test", 3); return r < 0 ? -errno : r; }
static long read_at(int fd, void* p, unsigned long len, unsigned long off) {
    if (!interrupted++) return -EINTR;
    long r = pread(fd, p, len, off); return r < 0 ? -errno : r;
}
static long write_short(int fd, const void* p, unsigned long len) {
    long r = write(fd, p, len > 7 ? 7 : len); return r < 0 ? -errno : r;
}
static long seal_image(int fd) { return fcntl(fd, F_ADD_SEALS, F_SEAL_SEAL | F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK) ? -errno : 0; }
static void close_image(int fd) { close(fd); }
int main(void) {
    struct dc_image_ops ops = { size_of, create_image, read_at, write_short, seal_image, close_image };
    assert(dc_snapshot_image(-1, &ops) == -EBADF);
    char name[] = "/tmp/dc-image-test-XXXXXX";
    int original = mkstemp(name); assert(original >= 0); assert(!unlink(name));
    assert(dc_snapshot_image(original, &ops) == -EINVAL);
    char content[4096]; memset(content, 'A', sizeof(content)); assert(write(original, content, sizeof(content)) == sizeof(content));
    assert(lseek(original, 8, SEEK_SET) == 8);
    long copy = dc_snapshot_image(original, &ops); assert(copy >= 0);
    assert(lseek(original, 0, SEEK_CUR) == 8);
    assert(pwrite(original, "B", 1, 0) == 1);
    char value; assert(pread((int)copy, &value, 1, 0) == 1 && value == 'A');
    assert(pwrite((int)copy, "C", 1, 0) == -1 && errno == EPERM);
    assert(ftruncate((int)copy, 0) == -1 && errno == EPERM);
    assert(ftruncate((int)copy, sizeof(content) + 1) == -1 && errno == EPERM);
    assert(fcntl((int)copy, F_ADD_SEALS, F_SEAL_FUTURE_WRITE) == -1 && errno == EPERM);
    void* mapping = mmap(NULL, sizeof(content), PROT_READ | PROT_WRITE, MAP_SHARED, (int)copy, 0);
    assert(mapping == MAP_FAILED && errno == EPERM);
    assert(!ftruncate(original, 0)); assert(pread((int)copy, &value, 1, 0) == 1 && value == 'A');
    close((int)copy); close(original);
    puts("PASS immutable descriptor contents, write/grow/shrink/shared-map seals, short IO and source offset");
}
