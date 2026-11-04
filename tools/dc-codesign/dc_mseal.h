/* Replace a read-only file mapping with an anonymous snapshot before sealing.
 * mseal alone does not stop writes to the original inode. Anonymous backing also
 * prevents madvise from discarding COW pages and re-reading a modified file.
 * Callbacks return negative errno on failure; addresses use the native long ABI.
 */
#ifndef DC_MSEAL_H
#define DC_MSEAL_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
struct dc_seal_ops {
    long (*allocate)(unsigned long);
    long (*protect)(void*, unsigned long, int);
    long (*relocate)(void*, unsigned long, void*);
    void (*release)(void*, unsigned long);
    long (*seal)(void*, unsigned long);
};
static inline int dc_vm_error(long value) {
    return (unsigned long)value >= (unsigned long)-4095;
}
static inline long dc_snapshot_and_seal(void* address, unsigned long length, int prot,
                                       const struct dc_seal_ops* ops) {
    const unsigned long page = 4096; /* Darling's x86 guest page size. */
    if (!length || ((uintptr_t)address & (page - 1)) ||
        length > ~(unsigned long)0 - (page - 1) || !(prot & 1) || (prot & 2)) return -22;
    length = (length + page - 1) & ~(page - 1);
    long allocated = ops->allocate(length);
    if (dc_vm_error(allocated)) return allocated;
    void* snapshot = (void*)(uintptr_t)allocated;
    memcpy(snapshot, address, length);
    long result = ops->protect(snapshot, length, prot);
    if (result < 0) { ops->release(snapshot, length); return result; }
    result = ops->relocate(snapshot, length, address);
    if (dc_vm_error(result)) { ops->release(snapshot, length); return result; }
    return ops->seal(address, length);
}
#endif
