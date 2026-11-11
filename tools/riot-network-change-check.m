#include <CoreFoundation/CoreFoundation.h>
#include <SystemConfiguration/SystemConfiguration.h>
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <string.h>

/* Chromium's NetworkChangeNotifierMac reports ERR_NETWORK_CHANGED when a
 * notification fires while a request is in flight. This probe asks whether
 * Darling's SystemConfiguration raises such a notification on its own: it
 * watches 127.0.0.1 through SCNetworkReachability and watches the dynamic
 * store's State:/Network/Global/IPv4, then counts callbacks for a few seconds
 * without touching the network.

   A reachability callback with kSCNetworkReachabilityFlagsReachable means the
   path changed under the client with no user action; a dynamic-store callback
   means the same for the store watcher. Silence means the change Chromium
   reacts to comes from somewhere else. */

static int reachability_calls = 0;
static int store_calls = 0;

static void reachability_changed(SCNetworkReachabilityRef target, SCNetworkReachabilityFlags flags, void* info)
{
	reachability_calls++;
	printf("reachability callback %d: flags=0x%x reachable=%s\n", reachability_calls, flags,
		(flags & kSCNetworkReachabilityFlagsReachable) ? "yes" : "no");
	fflush(stdout);
}

static void store_changed(SCDynamicStoreRef store, CFArrayRef changed, void* info)
{
	store_calls++;
	printf("dynamic store callback %d: %ld key(s) changed\n", store_calls,
		changed ? (long)CFArrayGetCount(changed) : -1L);
	fflush(stdout);
}

int main(void)
{
	SCNetworkReachabilityRef reachability = SCNetworkReachabilityCreateWithName(NULL, "127.0.0.1");
	printf("SCNetworkReachabilityCreateWithName: %s\n", reachability ? "ok" : "NULL");

	if (reachability) {
		SCNetworkReachabilityContext context = { 0, NULL, NULL, NULL, NULL };
		Boolean set = SCNetworkReachabilitySetCallback(reachability, reachability_changed, &context);
		Boolean scheduled = SCNetworkReachabilityScheduleWithRunLoop(reachability,
			CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
		SCNetworkReachabilityFlags flags = 0;
		Boolean got = SCNetworkReachabilityGetFlags(reachability, &flags);
		printf("setCallback=%d schedule=%d getFlags=%d flags=0x%x\n",
			(int)set, (int)scheduled, (int)got, flags);
	} else {
		/* Chromium falls back to a polling notifier when this fails, which
		 * cannot produce ERR_NETWORK_CHANGED on its own. */
		printf("no reachability object: the client uses a polling notifier\n");
	}

	SCDynamicStoreRef store = SCDynamicStoreCreate(NULL, CFSTR("darling-network-change-check"), store_changed, NULL);
	printf("SCDynamicStoreCreate: %s\n", store ? "ok" : "NULL");
	if (store) {
		CFStringRef key = CFSTR("State:/Network/Global/IPv4");
		CFStringRef pattern = CFSTR("State:/Network/");
		CFArrayRef key_array = CFArrayCreate(NULL, (const void**)&key, 1, &kCFTypeArrayCallBacks);
		CFArrayRef pattern_array = CFArrayCreate(NULL, (const void**)&pattern, 1, &kCFTypeArrayCallBacks);
		Boolean set = SCDynamicStoreSetNotificationKeys(store, key_array, pattern_array);
		CFRunLoopSourceRef source = SCDynamicStoreCreateRunLoopSource(NULL, store, 0);
		printf("setNotificationKeys=%d runLoopSource=%s\n", (int)set, source ? "ok" : "NULL");
		if (source) {
			CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopDefaultMode);
		}
	}

	printf("watching for 5 seconds\n");
	fflush(stdout);
	CFRunLoopRunInMode(kCFRunLoopDefaultMode, 5.0, false);

	printf("result: reachability_callbacks=%d store_callbacks=%d verdict=%s\n",
		reachability_calls, store_calls,
		(reachability_calls || store_calls) ? "SPONTANEOUS-CHANGE" : "quiet");
	return (reachability_calls || store_calls) ? 1 : 0;
}
