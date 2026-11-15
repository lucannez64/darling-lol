#include <CoreVideo/CVDisplayLink.h>
#include <assert.h>
#include <stdio.h>
#include <stdatomic.h>
#include <unistd.h>

static _Atomic unsigned ticks[3];
static CVReturn tick(CVDisplayLinkRef link, const CVTimeStamp *now,
                     const CVTimeStamp *output, CVOptionFlags input,
                     CVOptionFlags *flags, void *context) {
    unsigned long index = (unsigned long)context;
    atomic_fetch_add_explicit(&ticks[index], 1, memory_order_relaxed);
    if (index == 2) assert(CVDisplayLinkStop(link) == 0);
    return kCVReturnSuccess;
}

int main(void) {
    CVDisplayLinkRef a, b;
    assert(CVDisplayLinkCreateWithCGDisplay(1, &a) == 0);
    assert(CVDisplayLinkCreateWithCGDisplay(1, &b) == 0);
    assert(CVDisplayLinkSetOutputCallback(a, tick, (void *)0) == 0);
    assert(CVDisplayLinkSetOutputCallback(b, tick, (void *)1) == 0);
    assert(CVDisplayLinkStart(a) == 0);
    printf("DISPLAY_LINK_ISOLATION second_running=%d\n", CVDisplayLinkIsRunning(b));
    fflush(stdout);
    assert(!CVDisplayLinkIsRunning(b));
    assert(CVDisplayLinkStart(b) == 0);
    usleep(300000);
    assert(ticks[0] > 0 && ticks[1] > 0);
    assert(CVDisplayLinkStop(a) == 0);
    unsigned stopped = ticks[0], before = ticks[1];
    usleep(150000);
    assert(ticks[0] == stopped && ticks[1] > before);
    CVDisplayLinkRelease(a);
    before = ticks[1];
    usleep(150000);
    assert(CVDisplayLinkIsRunning(b) && ticks[1] > before);
    // A reused slot and a reentrant stop must not disturb another link.
    CVDisplayLinkRef c;
    assert(CVDisplayLinkCreateWithCGDisplay(1, &c) == 0);
    assert(CVDisplayLinkSetOutputCallback(c, tick, (void *)2) == 0);
    assert(CVDisplayLinkStart(c) == 0);
    before = ticks[1];
    usleep(150000);
    assert(ticks[2] == 1 && !CVDisplayLinkIsRunning(c));
    assert(ticks[1] > before);
    CVDisplayLinkRelease(c);
    assert(CVDisplayLinkStop(b) == 0);
    CVDisplayLinkRelease(b);
    puts("DISPLAY_LINK_ISOLATION PASS: callbacks, stop and release are independent");
    return 0;
}
