/* darling-lol: AppKit's idle event wait must stay cheap and still wake promptly.
 *
 * Without windows, waits in -nextEventMatchingMask:untilDate: and checks the CPU
 * an idle second costs and how late main-queue blocks, run loop blocks with
 * CFRunLoopWakeUp, timers and events posted from another thread are handled.
 * Run with RCLOL_APPKIT_PORT_WAIT=0 for the polling wait it replaces. */
#import <AppKit/AppKit.h>
#include <dispatch/dispatch.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdio.h>
#include <sys/resource.h>
#include <unistd.h>

static double nowMs(void) {
    static mach_timebase_info_data_t scale;
    if (scale.denom == 0)
        mach_timebase_info(&scale);
    return mach_absolute_time() * (double) scale.numer / scale.denom / 1e6;
}

static double cpuMs(void) {
    struct rusage usage;
    getrusage(RUSAGE_SELF, &usage);
    return (usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) * 1e3 +
           (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1e3;
}

static volatile double posted, handled;

/* Waits up to one second for the background action to be handled; returns its latency. */
static double measure(NSApplication *app, void (^action)(void)) {
    posted = 0;
    handled = 0;
    dispatch_async(dispatch_get_global_queue(0, 0), ^{
        usleep(50000);
        posted = nowMs();
        action();
    });
    double start = nowMs();
    while (handled == 0 && nowMs() - start < 1000) {
        NSEvent *event = [app nextEventMatchingMask: NSAnyEventMask
                                          untilDate: [NSDate dateWithTimeIntervalSinceNow: 0.5]
                                             inMode: NSDefaultRunLoopMode
                                            dequeue: YES];
        if (event && [event type] == NSApplicationDefined && [event subtype] == 7)
            handled = nowMs();
    }
    return handled ? handled - posted : -1;
}

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        CFRunLoopRef mainLoop = CFRunLoopGetMain();

        /* Warm up the event path once. */
        [app nextEventMatchingMask: NSAnyEventMask untilDate: [NSDate distantPast]
                            inMode: NSDefaultRunLoopMode dequeue: YES];

        double cpu = cpuMs(), wall = nowMs();
        [app nextEventMatchingMask: NSAnyEventMask
                         untilDate: [NSDate dateWithTimeIntervalSinceNow: 1.0]
                            inMode: NSDefaultRunLoopMode
                           dequeue: YES];
        double idleCpu = cpuMs() - cpu, idleWall = nowMs() - wall;

        double dispatchLatency = measure(app, ^{
            dispatch_async(dispatch_get_main_queue(), ^{ handled = nowMs(); });
        });
        double wakeLatency = measure(app, ^{
            CFRunLoopPerformBlock(mainLoop, kCFRunLoopCommonModes, ^{ handled = nowMs(); });
            CFRunLoopWakeUp(mainLoop);
        });
        double postLatency = measure(app, ^{
            NSEvent *event = [NSEvent otherEventWithType: NSApplicationDefined
                                                location: NSZeroPoint
                                           modifierFlags: 0
                                               timestamp: 0
                                            windowNumber: 0
                                                 context: nil
                                                 subtype: 7
                                                   data1: 0
                                                   data2: 0];
            [app postEvent: event atStart: NO];
        });

        __block double fired = 0;
        double armed = nowMs();
        NSTimer *timer = [NSTimer timerWithTimeInterval: 0.1 repeats: NO block: ^(NSTimer *t) {
            (void) t;
            fired = nowMs();
        }];
        [[NSRunLoop currentRunLoop] addTimer: timer forMode: NSDefaultRunLoopMode];
        while (fired == 0 && nowMs() - armed < 1000)
            [app nextEventMatchingMask: NSAnyEventMask
                             untilDate: [NSDate dateWithTimeIntervalSinceNow: 0.5]
                                inMode: NSDefaultRunLoopMode
                               dequeue: YES];
        double timerLate = fired ? fired - armed - 100 : -1;

        int pass = idleWall >= 990 && dispatchLatency >= 0 && dispatchLatency < 20 &&
                   wakeLatency >= 0 && wakeLatency < 20 && postLatency >= 0 && postLatency < 20 &&
                   timerLate >= -1 && timerLate < 20;
        printf("APPKIT_IDLE_WAIT idle_cpu_ms=%.1f idle_wall_ms=%.0f dispatch_ms=%.2f wake_ms=%.2f "
               "post_ms=%.2f timer_late_ms=%.2f pass=%d\n",
               idleCpu, idleWall, dispatchLatency, wakeLatency, postLatency, timerLate, pass);
        return pass ? 0 : 1;
    }
}
