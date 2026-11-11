/* Standalone system-identity observations; no game, client, or account access.
 *
 * It reads only the identity facts an anti-cheat module of MVG's shape can read
 * about the host: the OS product version from three independent sources, the
 * CPU brand/vendor from the sysctl identity table AND from a direct CPUID, the
 * machine model, and the CPU core counts. It then records the cross-source
 * invariants that hold on every genuine Mac (one OS version everywhere; the
 * CPUID vendor equals the sysctl vendor; a real build string). It does not
 * decide an anti-cheat verdict and transmits nothing.
 *
 * macOS (CI ground truth, Apple-silicon runners skip the CPUID block):
 *   clang -O2 -Wall -Wextra -framework Foundation \
 *         identity_consistency.m -o identity_consistency
 *
 * Darling (live guest view): tools/run-identity-consistency.py
 */
#import <Foundation/Foundation.h>
#include <stdio.h>
#include <string.h>
#include <sys/sysctl.h>

static NSString *sysctlString(const char *name)
{
    size_t len = 0;
    if (sysctlbyname(name, NULL, &len, NULL, 0) != 0 || len == 0) return nil;
    char *buf = (char *)malloc(len);
    if (!buf) return nil;
    NSString *result = nil;
    if (sysctlbyname(name, buf, &len, NULL, 0) == 0)
        result = [NSString stringWithUTF8String:buf];
    free(buf);
    return result;
}

static NSNumber *sysctlInt(const char *name)
{
    int64_t value = 0;
    size_t len = sizeof(value);
    if (sysctlbyname(name, &value, &len, NULL, 0) != 0) return nil;
    return @(value);
}

#if defined(__x86_64__) || defined(__i386__)
#include <cpuid.h>
/* Vendor string from CPUID leaf 0 (EBX,EDX,ECX), executed in THIS process. A
 * per-process register spoof that only rewrites specific binaries' CPUID sites
 * does not reach this instruction. */
static NSString *cpuidVendor(void)
{
    unsigned a, b, c, d;
    if (!__get_cpuid(0, &a, &b, &c, &d)) return nil;
    char v[13];
    memcpy(v + 0, &b, 4);
    memcpy(v + 4, &d, 4);
    memcpy(v + 8, &c, 4);
    v[12] = '\0';
    return [NSString stringWithUTF8String:v];
}
/* Brand string from CPUID leaves 0x80000002..4. */
static NSString *cpuidBrand(void)
{
    unsigned a, b, c, d;
    if (!__get_cpuid(0x80000000u, &a, &b, &c, &d) || a < 0x80000004u) return nil;
    char brand[49];
    unsigned *w = (unsigned *)brand;
    for (unsigned leaf = 0x80000002u; leaf <= 0x80000004u; leaf++) {
        if (!__get_cpuid(leaf, &a, &b, &c, &d)) return nil;
        *w++ = a; *w++ = b; *w++ = c; *w++ = d;
    }
    brand[48] = '\0';
    char *p = brand; while (*p == ' ') p++;
    return [NSString stringWithUTF8String:p];
}
#endif

static NSString *orNull(NSString *s) { return s ? s : (NSString *)@"unavailable"; }

int main(void)
{
    @autoreleasepool {
        /* 1. OS version, three independent sources. */
        NSDictionary *plist = [NSDictionary dictionaryWithContentsOfFile:
            @"/System/Library/CoreServices/SystemVersion.plist"];
        NSString *plistVersion = plist[@"ProductVersion"];
        NSString *plistBuild = plist[@"ProductBuildVersion"];

        NSOperatingSystemVersion ov = [[NSProcessInfo processInfo] operatingSystemVersion];
        NSString *nsVersion = [NSString stringWithFormat:@"%ld.%ld.%ld",
            (long)ov.majorVersion, (long)ov.minorVersion, (long)ov.patchVersion];

        NSString *sysctlVersion = sysctlString("kern.osproductversion");
        NSString *sysctlBuild = sysctlString("kern.osversion");

        /* 2. CPU identity: sysctl table vs. a direct CPUID in this process. */
        NSString *sysctlBrand = sysctlString("machdep.cpu.brand_string");
        NSString *sysctlVendor = sysctlString("machdep.cpu.vendor");
        NSString *model = sysctlString("hw.model");

        NSString *directVendor = nil, *directBrand = nil;
#if defined(__x86_64__) || defined(__i386__)
        directVendor = cpuidVendor();
        directBrand = cpuidBrand();
#endif

        /* 3. Core counts (a real machine model implies a fixed count). */
        NSNumber *physical = sysctlInt("hw.physicalcpu_max");
        NSNumber *logical = sysctlInt("hw.logicalcpu_max");
        NSNumber *packages = sysctlInt("hw.packages");

        /* 4. Invariants that hold on every genuine Mac. */
        NSMutableArray *anomalies = [NSMutableArray array];
        BOOL osAgree = plistVersion && nsVersion && sysctlVersion &&
            [plistVersion isEqualToString:nsVersion] &&
            [plistVersion isEqualToString:sysctlVersion];
        if (!osAgree)
            [anomalies addObject:@"os_version_sources_disagree"];

        /* A real ProductBuildVersion is an alphanumeric Apple build (e.g.
         * 24G90), never a vendor name. */
        BOOL buildPlausible = plistBuild && plistBuild.length >= 4 &&
            [plistBuild rangeOfCharacterFromSet:[NSCharacterSet decimalDigitCharacterSet]].location != NSNotFound &&
            [[plistBuild lowercaseString] rangeOfString:@"darling"].location == NSNotFound;
        if (!buildPlausible)
            [anomalies addObject:@"build_string_not_a_real_build"];

        BOOL cpuidChecked = NO, cpuidVendorAgree = YES;
        if (directVendor && sysctlVendor) {
            cpuidChecked = YES;
            cpuidVendorAgree = [directVendor isEqualToString:sysctlVendor];
            if (!cpuidVendorAgree)
                [anomalies addObject:@"cpuid_vendor_disagrees_with_sysctl"];
        }
        BOOL cpuidBrandAgree = YES;
        if (directBrand && sysctlBrand) {
            /* Compare the leading vendor token (Intel/AMD/Apple). */
            NSString *dv = [[directBrand componentsSeparatedByString:@" "] firstObject];
            cpuidBrandAgree = [sysctlBrand rangeOfString:dv options:NSCaseInsensitiveSearch].location != NSNotFound;
            if (!cpuidBrandAgree)
                [anomalies addObject:@"cpuid_brand_disagrees_with_sysctl"];
        }

        NSDictionary *report = @{
            @"schema": @1,
            @"probe": @"identity_consistency",
            @"arch":
#if defined(__x86_64__)
                @"x86_64",
#elif defined(__arm64__)
                @"arm64",
#else
                @"other",
#endif
            @"os_version": @{
                @"system_version_plist": orNull(plistVersion),
                @"product_build_version": orNull(plistBuild),
                @"ns_process_info": orNull(nsVersion),
                @"kern_osproductversion": orNull(sysctlVersion),
                @"kern_osversion": orNull(sysctlBuild),
            },
            @"cpu": @{
                @"machdep_brand_string": orNull(sysctlBrand),
                @"machdep_vendor": orNull(sysctlVendor),
                @"cpuid_vendor_in_process": orNull(directVendor),
                @"cpuid_brand_in_process": orNull(directBrand),
                @"hw_model": orNull(model),
                @"hw_physicalcpu_max": physical ?: (id)[NSNull null],
                @"hw_logicalcpu_max": logical ?: (id)[NSNull null],
                @"hw_packages": packages ?: (id)[NSNull null],
            },
            @"consistency": @{
                @"os_version_sources_agree": @(osAgree),
                @"build_string_plausible": @(buildPlausible),
                @"cpuid_vendor_checked": @(cpuidChecked),
                @"cpuid_vendor_agrees_with_sysctl": @(cpuidVendorAgree),
                @"cpuid_brand_agrees_with_sysctl": @(cpuidBrandAgree),
            },
            @"anomalies": anomalies,
            @"anomaly_count": @(anomalies.count),
        };

        NSError *error = nil;
        NSData *output = [NSJSONSerialization dataWithJSONObject:report
            options:NSJSONWritingPrettyPrinted error:&error];
        if (!output) { fprintf(stderr, "Cannot serialize report\n"); return 2; }
        fwrite([output bytes], 1, [output length], stdout);
        putchar('\n');
    }
    return 0;
}
