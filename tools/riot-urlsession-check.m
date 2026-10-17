// Standalone CFNetwork regression: only the host runner's loopback fixture is used.
#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#include <stdio.h>
#include <string.h>
static NSString *probeURL;
static NSString *failureURL;

static int session_check(NSString *url, int cancelled) {
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    __block int ok = 0;
    __block int callbacks = 0;
    NSURLSessionDataTask *task = [[NSURLSession sharedSession] dataTaskWithURL:[NSURL URLWithString:url] completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        __sync_add_and_fetch(&callbacks, 1);
        if (cancelled == 1) ok = error && [error code] == NSURLErrorCancelled;
        else if (cancelled == 2) ok = error != nil;
        else ok = !error && [data length] == 18 && memcmp([data bytes], "darling-session-ok", 18) == 0 && [(NSHTTPURLResponse *)response statusCode] == 200;
        dispatch_semaphore_signal(sem);
    }];
    if (!task || [task state] != NSURLSessionTaskStateSuspended) return 1;
    if (cancelled == 1) { [task cancel]; [task cancel]; }
    [task resume]; [task resume];
    if (dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC))) { printf("TIMEOUT\n"); return 2; }
    if (!ok || callbacks != 1 || [task state] != NSURLSessionTaskStateCompleted) {printf("FAIL callback=%d ok=%d\n",callbacks,ok);return 3;}
    return 0;
}
static dispatch_semaphore_t invalidated;
static volatile int invalidationCount;
@interface LifecycleProbe : NSObject <NSURLSessionDelegate>
@end
@implementation LifecycleProbe
- (void)URLSession:(NSURLSession *)session didBecomeInvalidWithError:(NSError *)error {
    __sync_add_and_fetch(&invalidationCount, 1);
    dispatch_semaphore_signal(invalidated);
}
@end
static int lifecycle_check(int cancel) {
    invalidated = dispatch_semaphore_create(0);
    invalidationCount = 0;
    dispatch_semaphore_t completed = dispatch_semaphore_create(0);
    __block int ok = 0;
    LifecycleProbe *delegate = [[LifecycleProbe alloc] init];
    NSURLSession *session = [[NSURLSession sessionWithConfiguration:[NSURLSessionConfiguration defaultSessionConfiguration] delegate:delegate delegateQueue:nil] retain];
    NSURLSessionDataTask *task = [session dataTaskWithURL:[NSURL URLWithString:probeURL] completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        ok = cancel ? (error && [error code] == NSURLErrorCancelled) : (!error && [data length] == 18);
        dispatch_semaphore_signal(completed);
    }];
    if (cancel) { [session invalidateAndCancel]; [session invalidateAndCancel]; }
    else { [task resume]; [session finishTasksAndInvalidate]; [session finishTasksAndInvalidate]; }
    if (dispatch_semaphore_wait(completed, dispatch_time(DISPATCH_TIME_NOW, 15*NSEC_PER_SEC))) return 10;
    if (dispatch_semaphore_wait(invalidated, dispatch_time(DISPATCH_TIME_NOW, 15*NSEC_PER_SEC))) return 11;
    if (!ok || invalidationCount != 1) return 12;
    [session release]; [delegate release];
    return 0;
}
int main(int argc, const char **argv) { @autoreleasepool {
 if (argc != 3) { puts("Usage: probe <loopback-success-url> <loopback-error-url>"); return 64; }
 probeURL = [NSString stringWithUTF8String:argv[1]];
 failureURL = [NSString stringWithUTF8String:argv[2]];
 for (int i=0;i<5;i++) if (session_check(probeURL,0)) return 1;
 if (session_check(probeURL,1)) return 2;
 if (session_check(failureURL,2)) return 3;
 if (lifecycle_check(0) || lifecycle_check(1)) { puts("LIFECYCLE_FAIL"); return 4; }
 puts("LIFECYCLE_PASS: finish waits for request; cancel delivers callback; invalidation once");
 puts("URLSESSION_PASS: 5 HTTP requests and cancel/resume, callbacks exactly once");
 return 0;
}}
