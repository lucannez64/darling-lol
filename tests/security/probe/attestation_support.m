// Reports the macOS facilities a key-pair/attestation support check can depend on.
// Read-only apart from one throwaway Secure Enclave key, which is never stored.
// Build: clang -fobjc-arc -framework Foundation -framework Security
//        -framework DeviceCheck -framework LocalAuthentication -o attestation_support attestation_support.m
#import <DeviceCheck/DeviceCheck.h>
#import <Foundation/Foundation.h>
#import <LocalAuthentication/LocalAuthentication.h>
#import <Security/Security.h>
#include <sys/sysctl.h>

static NSString *sysctl_string(const char *name) {
    char buf[256] = {0};
    size_t len = sizeof buf;
    return sysctlbyname(name, buf, &len, NULL, 0) == 0 ? @(buf) : @"";
}

int main(void) {
    @autoreleasepool {
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        out[@"hw_model"] = sysctl_string("hw.model");
        out[@"cpu_brand"] = sysctl_string("machdep.cpu.brand_string");
        out[@"os_version"] = [[NSProcessInfo processInfo] operatingSystemVersionString];

        if (@available(macOS 11.0, *))
            out[@"app_attest_supported"] = @(DCAppAttestService.sharedService.isSupported);
        out[@"devicecheck_supported"] = @(DCDevice.currentDevice.isSupported);

        LAContext *la = [LAContext new];
        NSError *laError = nil;
        out[@"la_biometrics"] = @([la canEvaluatePolicy:LAPolicyDeviceOwnerAuthenticationWithBiometrics error:&laError]);
        out[@"la_biometrics_error"] = laError ? @(laError.code) : @0;

        CFErrorRef acError = NULL;
        SecAccessControlRef ac = SecAccessControlCreateWithFlags(
            NULL, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, kSecAccessControlPrivateKeyUsage, &acError);
        NSDictionary *attrs = @{
            (id)kSecAttrKeyType: (id)kSecAttrKeyTypeECSECPrimeRandom,
            (id)kSecAttrKeySizeInBits: @256,
            (id)kSecAttrTokenID: (id)kSecAttrTokenIDSecureEnclave,
            (id)kSecPrivateKeyAttrs: @{(id)kSecAttrIsPermanent: @NO, (id)kSecAttrAccessControl: (__bridge id)ac},
        };
        CFErrorRef keyError = NULL;
        SecKeyRef key = SecKeyCreateRandomKey((__bridge CFDictionaryRef)attrs, &keyError);
        out[@"secure_enclave_key"] = @(key != NULL);
        out[@"secure_enclave_error"] = keyError ? @(CFErrorGetCode(keyError)) : @0;
        if (key) CFRelease(key);
        if (keyError) CFRelease(keyError);
        if (ac) CFRelease(ac);

        NSDictionary *soft = @{(id)kSecAttrKeyType: (id)kSecAttrKeyTypeECSECPrimeRandom, (id)kSecAttrKeySizeInBits: @256};
        CFErrorRef softError = NULL;
        SecKeyRef softKey = SecKeyCreateRandomKey((__bridge CFDictionaryRef)soft, &softError);
        out[@"software_p256_key"] = @(softKey != NULL);
        if (softKey) CFRelease(softKey);
        if (softError) CFRelease(softError);

        NSData *json = [NSJSONSerialization dataWithJSONObject:out
                                                       options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys
                                                         error:nil];
        fwrite(json.bytes, 1, json.length, stdout);
        putchar('\n');
    }
    return 0;
}
