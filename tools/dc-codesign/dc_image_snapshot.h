/* Immutable file snapshots bind verification and mapping to the same bytes.
 * Callbacks normalize errors to negative errno and never change the source offset.
 */
#ifndef DC_IMAGE_SNAPSHOT_H
#define DC_IMAGE_SNAPSHOT_H
#include <stddef.h>
enum { DC_F_SNAPSHOT_IMAGE = 0x40004443 };
struct dc_image_ops {
    long (*size)(int);
    long (*create)(void);
    long (*read)(int, void*, unsigned long, unsigned long);
    long (*write)(int, const void*, unsigned long);
    long (*seal)(int);
    void (*close)(int);
};
static inline long dc_snapshot_image(int source, const struct dc_image_ops* ops) {
    long length = ops->size(source);
    if (length < 0) return length;
    if (!length) return -22;
    if ((unsigned long)length > 2UL * 1024 * 1024 * 1024) return -27;
    long target = ops->create();
    if (target < 0) return target;
    unsigned long offset = 0;
    long error = -5;
    char buffer[16384];
    while (offset < (unsigned long)length) {
        unsigned long amount = (unsigned long)length - offset;
        if (amount > sizeof(buffer)) amount = sizeof(buffer);
        long got = ops->read(source, buffer, amount, offset);
        if (got == -4) continue;
        if (got <= 0) { error = got < 0 ? got : -5; goto fail; }
        unsigned long written = 0;
        while (written < (unsigned long)got) {
            long put = ops->write((int)target, buffer + written, (unsigned long)got - written);
            if (put == -4) continue;
            if (put <= 0) { error = put < 0 ? put : -5; goto fail; }
            written += (unsigned long)put;
        }
        offset += (unsigned long)got;
    }
    error = ops->seal((int)target);
    if (error < 0) goto fail;
    return target;
fail:
    ops->close((int)target);
    return error;
}
#endif
