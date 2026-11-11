/* Standalone Foundation/dispatch observations; no game or account access.
 * macOS: clang -O2 -Wall -Wextra -fblocks -framework Foundation 
 *        foundation_compat.m -o foundation_compat
 */
#import <Foundation/Foundation.h>
#include <dispatch/dispatch.h>
#include <stdio.h>

int main(void)
{
    @autoreleasepool {
        NSMutableArray *cases = [NSMutableArray array];
        int failures = 0;
        /* Values are synthetic. Above 2^53 is observational: do not assume
         * every Foundation version preserves arbitrary JSON integers. */
        const unsigned long long values[] = {0, 1, 4294967297ULL,
            9007199254740991ULL, 9007199254740993ULL, 9223372036854775807ULL};
        for (unsigned i = 0; i < sizeof(values) / sizeof(values[0]); i++) {
            NSString *input = [NSString stringWithFormat:
                @"{\"data\":{\"id\":%llu},\"phase\":\"Running\"}", values[i]];
            NSError *error = nil;
            id decoded = [NSJSONSerialization JSONObjectWithData:
                [input dataUsingEncoding:NSUTF8StringEncoding] options:0 error:&error];
            id number = [[decoded objectForKey:@"data"] objectForKey:@"id"];
            BOOL numeric = [number isKindOfClass:[NSNumber class]];
            BOOL exact = numeric && [number unsignedLongLongValue] == values[i];
            NSData *encoded = decoded ? [NSJSONSerialization dataWithJSONObject:
                decoded options:0 error:&error] : nil;
            id roundtrip = encoded ? [NSJSONSerialization JSONObjectWithData:
                encoded options:0 error:&error] : nil;
            id again = [[roundtrip objectForKey:@"data"] objectForKey:@"id"];
            BOOL roundtripExact = [again isKindOfClass:[NSNumber class]] &&
                [again unsignedLongLongValue] == values[i];
            BOOL required = values[i] <= 9007199254740991ULL;
            if (required && (!exact || !roundtripExact)) failures++;
            [cases addObject:@{@"input": [NSString stringWithFormat:@"%llu", values[i]],
                @"numeric": @(numeric), @"exact": @(exact),
                @"roundtrip_exact": @(roundtripExact), @"required": @(required),
                @"observed": numeric ? [number stringValue] : @"unavailable",
                @"error": error ? [error localizedDescription] : @""}];
        }
        /* All accesses to next/mismatches occur on this queue. The final
         * sync waits for every preceding async block; no timing assumption. */
        dispatch_queue_t queue = dispatch_queue_create("org.darling.compat.fifo", DISPATCH_QUEUE_SERIAL);
        __block unsigned next = 0, mismatches = 0;
        for (unsigned i = 0; i < 1000; i++) {
            dispatch_async(queue, ^{ if (next != i) mismatches++; next++; });
        }
        dispatch_sync(queue, ^{});
        if (next != 1000 || mismatches) failures++;
        NSDictionary *report = @{@"schema": @1, @"probe": @"foundation_compat",
            @"json_integers": cases, @"serial_fifo": @{@"completed": @(next),
                @"mismatches": @(mismatches)}, @"failures": @(failures)};
        NSError *error = nil;
        NSData *output = [NSJSONSerialization dataWithJSONObject:report
            options:NSJSONWritingPrettyPrinted error:&error];
        if (!output) { fprintf(stderr, "Cannot serialize report\n"); return 2; }
        fwrite([output bytes], 1, [output length], stdout);
        putchar('\n');
        dispatch_release(queue);
        return failures ? 1 : 0;
    }
}
