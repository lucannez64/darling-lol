// How an idle AppKit main thread waits, and how fast work interrupts the wait.
//
// The main thread sits in -nextEventMatchingMask:untilDate: with nothing to do, the way an idle Electron client
// does. A run-loop observer counts run-loop passes (each pass is darlingserver round trips). Then other threads
// hand the main thread work through the three paths an app uses: a main-queue block (dispatch_async), a version-0
// run-loop source signalled with CFRunLoopWakeUp (Chromium's message pump), and a run-loop timer.
//
// Expected with the kevent wait: few idle passes per second (the 2 ms ppoll gave about 500), and every latency
// well under the 16 ms idle slice. Prints one RUNLOOP_IDLE line.
#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#include <dispatch/dispatch.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

static double nowMs(void) {
	static mach_timebase_info_data_t scale;
	if (scale.denom == 0) mach_timebase_info(&scale);
	return mach_absolute_time() * (double)scale.numer / scale.denom / 1e6;
}

static unsigned passes;
static volatile double sourcePosted, sourceRan, timerDue, timerRan;

static void countPass(CFRunLoopObserverRef observer, CFRunLoopActivity activity, void *info) {
	++passes;
}

static void sourcePerform(void *info) {
	sourceRan = nowMs();
}

static void timerFired(CFRunLoopTimerRef timer, void *info) {
	timerRan = nowMs();
}

static volatile double dispatchPosted, dispatchRan;

static void *postWork(void *source) {
	usleep(300 * 1000);
	dispatchPosted = nowMs();
	dispatch_async(dispatch_get_main_queue(), ^{ dispatchRan = nowMs(); });
	usleep(400 * 1000);
	sourcePosted = nowMs();
	CFRunLoopSourceSignal((CFRunLoopSourceRef)source);
	CFRunLoopWakeUp(CFRunLoopGetMain());
	return NULL;
}

// Run the AppKit event wait for `seconds`, like an idle app's main loop.
static void idle(double seconds) {
	double end = nowMs() + seconds * 1000.0;
	while (nowMs() < end) {
		@autoreleasepool {
			[NSApp nextEventMatchingMask:NSAnyEventMask
			                   untilDate:[NSDate dateWithTimeIntervalSinceNow:(end - nowMs()) / 1000.0]
			                      inMode:NSDefaultRunLoopMode
			                     dequeue:YES];
		}
	}
}

int main(void) {
	@autoreleasepool {
		[NSApplication sharedApplication];
		CFRunLoopRef main = CFRunLoopGetMain();

		CFRunLoopObserverRef observer = CFRunLoopObserverCreate(NULL, kCFRunLoopEntry, true, 0, countPass, NULL);
		CFRunLoopAddObserver(main, observer, kCFRunLoopCommonModes);

		CFRunLoopSourceContext context = {0};
		context.perform = sourcePerform;
		CFRunLoopSourceRef source = CFRunLoopSourceCreate(NULL, 0, &context);
		CFRunLoopAddSource(main, source, kCFRunLoopCommonModes);

		// settle (the slice grows only after 100 ms without work), then measure the idle pass rate
		idle(0.3);
		passes = 0;
		double idleStart = nowMs();
		idle(2.0);
		double idleRate = passes / ((nowMs() - idleStart) / 1000.0);

		// a main-queue block, then Chromium's path (signal a version-0 source and wake the run loop), both posted from
		// a plain thread while the main thread is idle
		pthread_t poster;
		pthread_create(&poster, NULL, postWork, (void *)source);
		// a timer due 1.1 s from now
		timerDue = nowMs() + 1100.0;
		CFRunLoopTimerRef timer = CFRunLoopTimerCreate(NULL, CFAbsoluteTimeGetCurrent() + 1.1, 0, 0, 0, timerFired, NULL);
		CFRunLoopAddTimer(main, timer, kCFRunLoopCommonModes);
		idle(1.5);

		pthread_join(poster, NULL);
		double dispatchMs = dispatchRan > 0 ? dispatchRan - dispatchPosted : -1;
		double sourceMs = sourceRan > 0 ? sourceRan - sourcePosted : -1;
		double timerLateMs = timerRan > 0 ? timerRan - timerDue : -1;
		BOOL ok = idleRate < 150 && dispatchMs >= 0 && dispatchMs < 8 && sourceMs >= 0 && sourceMs < 8 &&
			timerRan > 0 && timerLateMs > -2 && timerLateMs < 8;
		printf("RUNLOOP_IDLE idle_passes_per_s=%.0f dispatch_ms=%.2f source_ms=%.2f timer_late_ms=%.2f posted=%d,%d ok=%d\n",
			idleRate, dispatchMs, sourceMs, timerLateMs, dispatchPosted > 0, sourcePosted > 0, ok);
		fflush(stdout);
		return ok ? 0 : 1;
	}
}
