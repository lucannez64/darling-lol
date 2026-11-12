/* Guest check: a URL opened with LaunchServices reaches the running app that declares its scheme.
 *
 * On macOS, LaunchServices delivers a URL whose scheme an app declares in
 * CFBundleURLTypes to that running app as a kInternetEventClass/kAEGetURL Apple
 * event, or to -application:openURLs: when no handler is registered. The Riot
 * Client completes Google/social sign-in that way (riotclient://auth/...).
 * Darling had no such delivery: NSAppleEventManager only printed STUB and
 * LaunchServices rejected every scheme but http, https, mailto and file.
 *
 * tools/build-riot-url-event-check.py builds RiotURLEventCheck.app. Its Info.plist
 * declares the schemes darling-lol-ae and darling-lol-delegate. Run the bundle
 * executable with no arguments inside the guest (DISPLAY must reach an X server):
 *   ae        an app registered with NSAppleEventManager gets darling-lol-ae://... as
 *             typeUnicodeText ('utxt'), as +descriptorWithString: creates on macOS
 *   delegate  an app without a handler gets darling-lol-delegate://... in application:openURLs:
 *   undeclared  a scheme no running app declares returns kLSApplicationNotFoundErr
 *   nopath      with no serving app, open(1)'s path (LSOpenURLsWithRole) must not take
 *               darling-lol-nobody:///path/Fake.app for a bundle path and launch it
 *   control     raw data sent straight to the app's port is still checked: a control
 *               character, or a URL of another scheme, is refused and never delivered
 * Prints one line per case and exits 0 when all pass.
 */
#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#include <CoreServices/CoreServices.h>
#include <spawn.h>
#include <signal.h>
#include <sys/wait.h>

extern char **environ;

enum { kGURL = 'GURL', kDirectObject = '----' };

/* ---- the app side ------------------------------------------------------- */

static NSString *resultPath;

static void record(NSString *how, NSString *url)
{
	NSString *line = [NSString stringWithFormat:@"%@ %@\n", how, url];
	NSFileHandle *file = [NSFileHandle fileHandleForWritingAtPath:resultPath];
	[file seekToEndOfFile];
	[file writeData:[line dataUsingEncoding:NSUTF8StringEncoding]];
	[file closeFile];
}

@interface CheckDelegate : NSObject
@property (nonatomic) BOOL useAppleEvent;
@end

@implementation CheckDelegate
- (void)applicationWillFinishLaunching:(NSNotification *)note
{
	if (self.useAppleEvent)
		[[NSAppleEventManager sharedAppleEventManager] setEventHandler:self
		                                                   andSelector:@selector(handleGetURLEvent:withReplyEvent:)
		                                                 forEventClass:kGURL
		                                                    andEventID:kGURL];
}
- (void)applicationDidFinishLaunching:(NSNotification *)note
{
	record(@"ready", @"-");
}
- (void)handleGetURLEvent:(NSAppleEventDescriptor *)event withReplyEvent:(NSAppleEventDescriptor *)reply
{
	NSAppleEventDescriptor *current = [[NSAppleEventManager sharedAppleEventManager] currentAppleEvent];
	NSAppleEventDescriptor *direct = [event paramDescriptorForKeyword:kDirectObject];
	NSString *how = current != event ? @"appleevent-without-current"
	              : [direct descriptorType] != 'utxt' ? @"appleevent-not-utxt" : @"appleevent";
	record(how, [direct stringValue]);
}
- (void)application:(NSApplication *)application openURLs:(NSArray *)urls
{
	for (NSURL *url in urls)
		record(@"openURLs", [url absoluteString]);
}
@end

static int serve(BOOL useAppleEvent)
{
	@autoreleasepool {
		NSApplication *app = [NSApplication sharedApplication];
		CheckDelegate *delegate = [CheckDelegate new];
		delegate.useAppleEvent = useAppleEvent;
		[app setDelegate:delegate];
		[app run];
	}
	return 0;
}

/* ---- the checker side --------------------------------------------------- */

static NSString *readResults(void)
{
	return [NSString stringWithContentsOfFile:resultPath encoding:NSUTF8StringEncoding error:NULL] ?: @"";
}

static BOOL waitFor(NSString *needle, double seconds)
{
	for (double waited = 0; waited < seconds; waited += 0.05) {
		if ([readResults() rangeOfString:needle].location != NSNotFound)
			return YES;
		usleep(50000);
	}
	return NO;
}

static OSStatus openURL(NSString *string)
{
	NSURL *url = [NSURL URLWithString:string];
	return url ? LSOpenCFURLRef((CFURLRef) url, NULL) : paramErr;
}

/* Reply byte from the app's port, or -1 when nothing answered. */
static int sendRaw(NSString *scheme, const char *bytes)
{
	CFStringRef name = (CFStringRef) [@"org.darlinghq.url-event." stringByAppendingString:scheme];
	CFMessagePortRef port = CFMessagePortCreateRemote(NULL, name);
	if (!port)
		return -1;
	CFDataRef data = CFDataCreate(NULL, (const UInt8 *) bytes, strlen(bytes));
	CFDataRef reply = NULL;
	int result = -1;
	if (CFMessagePortSendRequest(port, 0, data, 2, 5, kCFRunLoopDefaultMode, &reply) == kCFMessagePortSuccess &&
	    reply && CFDataGetLength(reply) == 1)
		result = CFDataGetBytePtr(reply)[0];
	if (reply)
		CFRelease(reply);
	CFRelease(data);
	CFMessagePortInvalidate(port);
	CFRelease(port);
	return result;
}

static pid_t launch(const char *self, const char *mode)
{
	pid_t pid;
	char *argv[] = { (char *) self, "serve", (char *) mode, NULL };
	return posix_spawn(&pid, self, NULL, NULL, argv, environ) == 0 ? pid : -1;
}

static void stop(pid_t pid)
{
	if (pid > 0) {
		kill(pid, SIGKILL);
		waitpid(pid, NULL, 0);
	}
}

static int report(const char *name, BOOL ok, NSString *detail)
{
	printf("%-10s %s%s%s\n", name, ok ? "PASS" : "FAIL", detail ? " " : "", detail ? [detail UTF8String] : "");
	return !ok;
}

static int runCase(const char *self, const char *mode, NSString *scheme, NSString *expect)
{
	[@"" writeToFile:resultPath atomically:NO encoding:NSUTF8StringEncoding error:NULL];
	pid_t pid = launch(self, mode);
	if (pid < 0 || !waitFor(@"ready -", 20)) {
		stop(pid);
		return report(mode, NO, @"(app did not finish launching)");
	}
	NSString *url = [scheme stringByAppendingString:@"://auth/v1/check?x=1"];
	OSStatus status = openURL(url);
	BOOL ok = status == noErr && waitFor([NSString stringWithFormat:@"%@ %@\n", expect, url], 5);
	int failed = report(mode, ok, ok ? nil : [NSString stringWithFormat:@"(LSOpen %d, got: %@)", (int) status,
	                                          [readResults() stringByReplacingOccurrencesOfString:@"\n" withString:@" | "]]);

	if (!strcmp(mode, "ae")) {
		/* Extra cases while an app serves darling-lol-ae. */
		OSStatus none = openURL(@"darling-lol-nobody://x");
		failed += report("undeclared", none == kLSApplicationNotFoundErr,
		                 none == kLSApplicationNotFoundErr ? nil : [NSString stringWithFormat:@"(LSOpen %d)", (int) none]);
		/* No app serves darling-lol-nobody: open(1)'s path must not treat the URL's path
		 * as a bundle to launch (a fake bundle with no executable makes a launch fail). */
		NSString *fake = [NSTemporaryDirectory() stringByAppendingPathComponent:
		                  [NSString stringWithFormat:@"riot-url-event-check-%d/Fake.app", getpid()]];
		[[NSFileManager defaultManager] createDirectoryAtPath:[fake stringByAppendingPathComponent:@"Contents"]
		                          withIntermediateDirectories:YES attributes:nil error:NULL];
		[@{ @"CFBundleExecutable": @"Missing" } writeToFile:[fake stringByAppendingPathComponent:@"Contents/Info.plist"]
		                                         atomically:YES];
		NSURL *pathURL = [NSURL URLWithString:[@"darling-lol-nobody://" stringByAppendingString:fake]];
		OSStatus viaPath = LSOpenURLsWithRole((CFArrayRef) @[ pathURL ], kLSRolesAll, NULL, NULL, NULL, 0);
		[[NSFileManager defaultManager] removeItemAtPath:[fake stringByDeletingLastPathComponent] error:NULL];
		failed += report("nopath", viaPath == kLSApplicationNotFoundErr,
		                 viaPath == kLSApplicationNotFoundErr ? nil : [NSString stringWithFormat:@"(LSOpen %d)", (int) viaPath]);

		/* Any guest process can write to the port, so the app checks again. */
		NSString *before = readResults();
		BOOL refused = sendRaw(@"darling-lol-ae", "darling-lol-ae://x\x01y") == 0 &&
		               sendRaw(@"darling-lol-ae", "darling-lol-delegate://x") == 0;
		usleep(200000);
		failed += report("control", refused && [readResults() isEqualToString:before], nil);
	}
	stop(pid);
	return failed;
}

int main(int argc, char **argv)
{
	@autoreleasepool {
		resultPath = [NSTemporaryDirectory() stringByAppendingPathComponent:
		              [NSString stringWithFormat:@"riot-url-event-check-%d.txt", argc == 3 ? getppid() : getpid()]];
		if (argc == 3 && !strcmp(argv[1], "serve"))
			return serve(!strcmp(argv[2], "ae"));
		setvbuf(stdout, NULL, _IONBF, 0);
		int failed = runCase(argv[0], "ae", @"darling-lol-ae", @"appleevent")
		           + runCase(argv[0], "delegate", @"darling-lol-delegate", @"openURLs");
		[[NSFileManager defaultManager] removeItemAtPath:resultPath error:NULL];
		return failed != 0;
	}
}
