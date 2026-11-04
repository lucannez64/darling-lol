#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#include <stdio.h>

/* The exact layouts clang emits for constant literals on a macOS 11+ runtime.
 * Building them by hand here lets the check exercise the classes the same way a
 * prebuilt application does, without depending on clang's constant-literal
 * feature (which the Linux toolchain does not implement). */
struct const_int { Class isa; const char *encoding; long long value; };
struct const_dbl { Class isa; double value; };
struct const_flt { Class isa; float value; };
struct const_arr { Class isa; unsigned long count; const id *objects; };
struct const_dict { Class isa; unsigned long hashOptions; unsigned long count; const id *keys; const id *objects; };

extern struct const_arr __NSArray0__struct;
extern struct const_dict __NSDictionary0__struct;

static int failures;

static void expect_ok(const char *what, int ok)
{
    printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) failures++;
    fflush(stdout);
}

int main(void)
{
    @autoreleasepool {
        struct const_int n = { objc_getClass("NSConstantIntegerNumber"), "q", 42 };
        NSNumber *constant = (NSNumber *)&n;
        NSNumber *built = [NSNumber numberWithLongLong:42];
        expect_ok("integer value", [constant longLongValue] == 42);
        expect_ok("integer description", [[constant description] isEqualToString:@"42"]);
        expect_ok("integer equal to built number", [constant isEqualToNumber:built]);
        expect_ok("built number equal to constant", [built isEqualToNumber:constant]);
        expect_ok("integer survives retain/release",
              ([[constant retain] autorelease] == constant));

        struct const_int maximum = { objc_getClass("NSConstantIntegerNumber"), "Q", -1LL };
        NSNumber *unsignedConstant = (NSNumber *)&maximum;
        NSNumber *negative = [NSNumber numberWithLongLong:-1];
        NSNumber *zero = [NSNumber numberWithLongLong:0];
        expect_ok("signed negative is below unsigned maximum",
                  [negative compare:unsignedConstant] == NSOrderedAscending);
        expect_ok("signed zero is below unsigned maximum",
                  [zero compare:unsignedConstant] == NSOrderedAscending);
        expect_ok("unsigned maximum is not signed negative",
                  ![negative isEqualToNumber:unsignedConstant]);

        struct const_int same = { objc_getClass("NSConstantIntegerNumber"), "q", 42 };
        expect_ok("constant equality", [constant isEqualToNumber:(NSNumber *)&same]);
        expect_ok("constant hash matches built", [constant hash] == [built hash]);
        expect_ok("unsigned maximum orders above signed negative",
                  [unsignedConstant compare:negative] == NSOrderedDescending);
        expect_ok("unsigned maximum converts to positive double", [unsignedConstant doubleValue] > 0);
        expect_ok("empty array singleton layout", [(NSArray *)&__NSArray0__struct count] == 0);
        expect_ok("empty dictionary singleton layout", [(NSDictionary *)&__NSDictionary0__struct count] == 0);

        struct const_dbl d = { objc_getClass("NSConstantDoubleNumber"), 1.5 };
        NSNumber *dbl = (NSNumber *)&d;
        expect_ok("double value", [dbl doubleValue] == 1.5);
        expect_ok("double compares", [dbl compare:[NSNumber numberWithDouble:2.0]] == NSOrderedAscending);
        expect_ok("double not equal to 42", ![dbl isEqualToNumber:built]);

        struct const_dbl precise = { objc_getClass("NSConstantDoubleNumber"), 3.14159265 };
        NSNumber *preciseConstant = (NSNumber *)&precise;
        NSNumber *preciseBuilt = [NSNumber numberWithDouble:precise.value];
        expect_ok("double formatting matches built", [[preciseConstant stringValue]
                  isEqualToString:[preciseBuilt stringValue]]);
        expect_ok("double description matches built", [[preciseConstant description]
                  isEqualToString:[preciseBuilt description]]);
        struct const_flt preciseFloat = { objc_getClass("NSConstantFloatNumber"), 1234567.0f };
        expect_ok("float formatting matches built", [[(NSNumber *)&preciseFloat stringValue]
                  isEqualToString:[[NSNumber numberWithFloat:preciseFloat.value] stringValue]]);

        NSString *first = @"first";
        NSString *second = @"second";
        const id objects[2] = { first, second };
        struct const_arr a = { objc_getClass("NSConstantArray"), 2, objects };
        NSArray *array = (NSArray *)&a;
        expect_ok("array count", [array count] == 2);
        expect_ok("array object at index", [array objectAtIndex:1] == second);
        expect_ok("array contains", [array containsObject:first]);
        expect_ok("array equal to built array",
              [array isEqual:[NSArray arrayWithObjects:first, second, nil]]);
        expect_ok("array copy is self", [array copy] == array);

        const id keys[2] = { @"one", @"two" };
        const id values[2] = { @"1", @"2" };
        struct const_dict dc = { objc_getClass("NSConstantDictionary"), 0, 2, keys, values };
        NSDictionary *dictionary = (NSDictionary *)&dc;
        expect_ok("dictionary count", [dictionary count] == 2);
        expect_ok("dictionary lookup", [[dictionary objectForKey:@"two"] isEqualToString:@"2"]);
        expect_ok("dictionary missing key", [dictionary objectForKey:@"three"] == nil);
        expect_ok("dictionary keys", [[dictionary allKeys] count] == 2);
        expect_ok("dictionary equal to built dictionary",
              [dictionary isEqual:[NSDictionary dictionaryWithObjects:values forKeys:keys count:2]]);
        expect_ok("dictionary copy is self", [dictionary copy] == dictionary);

        id structArray = [NSString stringWithFormat:@"%@", array];
        expect_ok("array formats", [structArray length] > 0);
    }
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("PASS constant literals\n");
    return 0;
}
