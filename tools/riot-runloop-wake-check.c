/* darling-lol: can one poll() wait on Mach ports?
 *
 * The AppKit event wait polls the X11 socket in 2 ms slices because messages
 * that wake a run loop (CFRunLoopWakeUp, dispatch to the main queue) arrive on
 * Mach ports, which poll() cannot see. This probe checks that a kqueue with
 * EVFILT_MACHPORT gives a pollable descriptor that becomes readable when a
 * message reaches a port set member or the main-queue port, stays quiet
 * otherwise, and clears once the message is received. It uses only its own
 * ports and queues. */
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <poll.h>
#include <pthread.h>
#include <stdio.h>
#include <sys/event.h>
#include <unistd.h>

extern mach_port_t _dispatch_get_main_queue_port_4CF(void);
extern void _dispatch_main_queue_callback_4CF(void *msg);

static double nowMs(void) {
    static mach_timebase_info_data_t scale;
    if (scale.denom == 0)
        mach_timebase_info(&scale);
    return mach_absolute_time() * (double) scale.numer / scale.denom / 1e6;
}

static int watch(int kq, mach_port_t port) {
    struct kevent64_s change;
    EV_SET64(&change, port, EVFILT_MACHPORT, EV_ADD | EV_ENABLE, 0, 0, 0, 0, 0);
    return kevent64(kq, &change, 1, NULL, 0, 0, NULL);
}

/* poll() only reports the kqueue; reading its events rearms it. */
static int drain(int kq) {
    struct kevent64_s events[8];
    struct timespec zero = { 0, 0 };
    return kevent64(kq, NULL, 0, events, 8, 0, &zero);
}

static int readable(int fd, int timeoutMs) {
    struct pollfd entry = { .fd = fd, .events = POLLIN };
    return poll(&entry, 1, timeoutMs) > 0 && (entry.revents & POLLIN);
}

static mach_port_t member;
static volatile int ran;

static void *sendLater(void *unused) {
    (void) unused;
    usleep(50000);
    mach_msg_header_t message = {
        .msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0),
        .msgh_size = sizeof message,
        .msgh_remote_port = member,
        .msgh_id = 0x4c4f4c,
    };
    mach_msg(&message, MACH_SEND_MSG, sizeof message, 0, MACH_PORT_NULL, 0, MACH_PORT_NULL);
    return NULL;
}

static void markRan(void *unused) {
    (void) unused;
    ran = 1;
}

static void *dispatchLater(void *unused) {
    (void) unused;
    usleep(50000);
    dispatch_async_f(dispatch_get_main_queue(), NULL, markRan);
    return NULL;
}

int main(void) {
    mach_port_t task = mach_task_self(), set = MACH_PORT_NULL;
    int ok = mach_port_allocate(task, MACH_PORT_RIGHT_PORT_SET, &set) == KERN_SUCCESS &&
             mach_port_allocate(task, MACH_PORT_RIGHT_RECEIVE, &member) == KERN_SUCCESS &&
             mach_port_insert_right(task, member, member, MACH_MSG_TYPE_MAKE_SEND) == KERN_SUCCESS &&
             mach_port_insert_member(task, member, set) == KERN_SUCCESS;
    int kq = kqueue();
    int setWatched = ok && kq >= 0 && watch(kq, set) == 0;

    /* A port set member's message wakes poll() on the kqueue. */
    int setQuiet = setWatched && !readable(kq, 100);
    pthread_t thread;
    double start = nowMs();
    pthread_create(&thread, NULL, sendLater, NULL);
    int setWake = setWatched && readable(kq, 1000);
    double setLatency = nowMs() - start;
    pthread_join(thread, NULL);
    struct {
        mach_msg_header_t header;
        mach_msg_trailer_t trailer;
    } received;
    int setReceived = mach_msg(&received.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                               sizeof received, set, 0, MACH_PORT_NULL) == MACH_MSG_SUCCESS &&
                      received.header.msgh_id == 0x4c4f4c;
    int setDrained = drain(kq);
    int setCleared = setReceived && setDrained >= 0 && !readable(kq, 100);

    /* A dispatch to the main queue wakes poll() too, and clears once received. */
    mach_port_t mainPort = _dispatch_get_main_queue_port_4CF();
    int kqMain = kqueue();
    int mainWatched = mainPort != MACH_PORT_NULL && kqMain >= 0 && watch(kqMain, mainPort) == 0;
    int mainQuiet = mainWatched && !readable(kqMain, 100);
    start = nowMs();
    pthread_create(&thread, NULL, dispatchLater, NULL);
    int mainWake = mainWatched && readable(kqMain, 1000);
    double mainLatency = nowMs() - start;
    pthread_join(thread, NULL);
    struct {
        mach_msg_header_t header;
        char body[256];
    } wakeup;
    int mainReceived = mach_msg(&wakeup.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                                sizeof wakeup, mainPort, 0, MACH_PORT_NULL) == MACH_MSG_SUCCESS;
    if (mainReceived)
        _dispatch_main_queue_callback_4CF(&wakeup.header);
    int mainDrained = drain(kqMain);
    int mainCleared = mainReceived && mainDrained >= 0 && !readable(kqMain, 100);

    /* A second message after draining wakes poll() again. */
    pthread_create(&thread, NULL, sendLater, NULL);
    int setRewake = readable(kq, 1000);
    pthread_join(thread, NULL);

    /* Level: draining while that message is still queued must not lose it. */
    drain(kq);
    int setLevel = readable(kq, 200);
    setReceived = mach_msg(&received.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                           sizeof received, set, 0, MACH_PORT_NULL) == MACH_MSG_SUCCESS;
    drain(kq);
    int setLevelCleared = setReceived && !readable(kq, 100);
    printf("RUNLOOP_WAKE_LEVEL still_ready_after_drain=%d cleared_after_receive=%d\n",
           setLevel, setLevelCleared);

    /* CFRunLoop inserts the main-queue port into its wait set and extracts it on
     * every pass; the direct watch on that port must keep working afterwards. */
    mach_port_insert_member(task, mainPort, set);
    mach_port_extract_member(task, mainPort, set);
    drain(kqMain);
    ran = 0;
    pthread_create(&thread, NULL, dispatchLater, NULL);
    int mainAfterSet = readable(kqMain, 1000);
    pthread_join(thread, NULL);
    if (mach_msg(&wakeup.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof wakeup, mainPort, 0,
                 MACH_PORT_NULL) == MACH_MSG_SUCCESS)
        _dispatch_main_queue_callback_4CF(&wakeup.header);
    printf("RUNLOOP_WAKE_MEMBER main_wake_after_set=%d main_ran=%d\n", mainAfterSet, ran);

    int pass = mainAfterSet && setLevel && setLevelCleared && setQuiet && setWake && setReceived && setCleared &&
               mainQuiet && mainWake && mainReceived && ran && mainCleared && setRewake;
    printf("RUNLOOP_WAKE set_quiet=%d set_wake=%d set_latency_ms=%.1f set_drained=%d set_cleared=%d "
           "main_quiet=%d main_wake=%d main_latency_ms=%.1f main_ran=%d main_drained=%d "
           "main_cleared=%d set_rewake=%d pass=%d\n",
           setQuiet, setWake, setLatency, setDrained, setCleared, mainQuiet, mainWake, mainLatency,
           ran, mainDrained, mainCleared, setRewake, pass);
    return pass ? 0 : 1;
}
